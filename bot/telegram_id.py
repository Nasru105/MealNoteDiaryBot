"""Показать user ID из ожидающих личных сообщений, не печатая токен."""
from mealbot.config import Config
from mealbot.telegram import Telegram, TelegramError


def main():
    config = Config.read()
    if not config.token:
        raise RuntimeError('Сначала сохраните TELEGRAM_BOT_TOKEN в локальном .env.')
    updates = Telegram(config.token).call('getUpdates', timeout=0, allowed_updates=['message'])
    found = False
    for update in updates:
        message = update.get('message', {})
        if message.get('chat', {}).get('type') == 'private':
            print('user ID:', message.get('from', {}).get('id'), 'message ID:', message.get('message_id'))
            found = True
    if not found:
        print('Личных сообщений нет. Отправьте /start своему боту и повторите команду при остановленном боте.')


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, TelegramError) as exc:
        print('Ошибка:', exc)
