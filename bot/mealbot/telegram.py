"""Telegram Bot API, последовательный long polling без сторонних SDK."""
from datetime import datetime, timezone
import json
import logging
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .model import ModelError
from .formatting import render_reply
from .service import Message

log = logging.getLogger(__name__)


class TelegramError(RuntimeError):
    pass


class Telegram:
    def __init__(self, token):
        self.base = 'https://api.telegram.org/bot' + token + '/'

    def call(self, method, **data):
        request = Request(self.base + method, data=json.dumps(data).encode(),
                          headers={'Content-Type': 'application/json'})
        try:
            with urlopen(request, timeout=45) as response:
                result = json.load(response)
            if not result.get('ok'):
                raise TelegramError('Telegram не выполнил запрос.')
            return result['result']
        except HTTPError as exc:
            # Не печатать URL: в нём содержится токен.
            raise TelegramError(f'Telegram HTTP {exc.code}. Проверьте токен, сеть и webhook.') from None
        except (URLError, TimeoutError, OSError, ValueError):
            raise TelegramError('Нет связи с Telegram или неверный ответ сервера.') from None

    def send(self, chat_id, text):
        for part in render_reply(text):
            self.call('sendMessage', chat_id=chat_id, text=part,
                      parse_mode='MarkdownV2', link_preview_options={'is_disabled': True})


def run(service):
    config = service.config
    if not config.token or not config.allowed_user.isdecimal():
        raise RuntimeError('Заполните TELEGRAM_BOT_TOKEN и числовой ALLOWED_USER_ID в локальном .env.')
    api = Telegram(config.token)
    identity = api.call('getMe')
    # Не печатать токен, исходные update или сообщения пользователя в журнал.
    print('Бот подключён: @' + identity.get('username', '(без username)'))
    service.vault.initialize()
    while True:
        try:
            with service.vault.lock():
                state = service.vault.load()
                offset = state['telegram_offset']
            updates = api.call('getUpdates', offset=offset, timeout=25, allowed_updates=['message'])
            for update in updates:
                raw = update.get('message')
                if raw:
                    user_id = str(raw.get('from', {}).get('id', ''))
                    private = raw.get('chat', {}).get('type') == 'private'
                    # До доступа к модели и базе. Чужим пользователям ничего не отправляется.
                    if private and user_id == config.allowed_user:
                        chat_id = str(raw['chat']['id'])
                        if 'text' not in raw:
                            api.send(chat_id, 'Отправьте текстовое сообщение. Файлы и голосовые сообщения не обрабатываются.')
                        else:
                            try:
                                reply = service.handle(Message(
                                    'telegram', user_id, chat_id, str(raw['message_id']),
                                    datetime.fromtimestamp(raw['date'], timezone.utc), raw['text'], private))
                            except ModelError as exc:
                                # Не подтверждаем update: обработка повторится после восстановления модели.
                                log.warning('%s Повтор после восстановления; запись не подтверждена.', exc)
                                raise
                            api.send(chat_id, reply)
                # Сдвиг offset только после сохранения данных и успешной отправки ответа.
                with service.vault.lock():
                    state = service.vault.load()
                    state['telegram_offset'] = update['update_id'] + 1
                    service.vault.commit(state)
        except (TelegramError, ModelError) as exc:
            log.warning('%s Повтор через 5 секунд.', exc)
            time.sleep(5)
