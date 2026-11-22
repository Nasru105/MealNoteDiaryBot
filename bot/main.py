"""Запуск: python main.py init|console|telegram|script|export."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
import uuid
from zoneinfo import ZoneInfo

from mealbot.config import Config
from mealbot.model import ModelError
from mealbot.service import Message, Service


def main():
    parser = argparse.ArgumentParser(description='Дневник питания: Python + Ollama + Obsidian + Telegram')
    parser.add_argument('mode', choices=['init', 'console', 'telegram', 'script', 'export'])
    parser.add_argument('--demo', action='store_true', help='Учебный профиль и отдельное хранилище vault-demo')
    parser.add_argument('--vault', help='Путь к отдельному хранилищу')
    parser.add_argument('--input', help='JSONL-сообщения для локальной демонстрации')
    parser.add_argument('--output', help='Сохранить фактический диалог в JSON')
    args = parser.parse_args()
    config = Config.read(demo=args.demo, vault=args.vault)
    if args.mode == 'telegram' and args.demo:
        parser.error('--demo нельзя использовать для Telegram')
    if args.mode in ('console', 'script') and not args.demo and not config.allowed_user:
        parser.error('Для локальной демонстрации используйте --demo; для личной базы задайте ALLOWED_USER_ID.')
    service = Service(config)
    if args.mode in ('init', 'export'):
        service.vault.initialize()
        print('Хранилище готово: ' + str(config.vault))
    elif args.mode == 'telegram':
        from mealbot.telegram import run
        run(service)
    elif args.mode == 'console':
        print('Локальный диалог с настоящей Ollama. /help - справка, /exit - выход.')
        print('Хранилище: ' + str(config.vault))
        session = uuid.uuid4().hex[:12]
        index = 0
        while True:
            try:
                text = input('\nВы > ').strip()
            except EOFError:
                break
            if text == '/exit':
                break
            index += 1
            sent = datetime.now(ZoneInfo('Europe/Moscow'))  # Service использует пояс профиля.
            message = Message('console', config.allowed_user, config.allowed_user, f'{session}-{index}', sent, text)
            try:
                print('Бот > ' + service.handle(message))
            except ModelError as exc:
                print('Бот > ' + str(exc) + ' Успешное сохранение не подтверждено.')
    else:
        if not args.input:
            parser.error('Для script необходим --input с JSONL-сообщениями.')
        transcript = []
        # Это воспроизведение учебного разговора, не набор тестов: нет assert и оценщика.
        for line in Path(args.input).read_text(encoding='utf-8-sig').splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get('restart'):
                service = Service(config)
                print('\n[Повторное открытие хранилища]', flush=True)
                continue
            text = row['text']
            # Идентификатор из уже реально созданного предложения.
            if '{proposal}' in text:
                with service.vault.lock():
                    state = service.vault.load()
                    proposals = list(state['proposals'])
                if not proposals:
                    raise RuntimeError('Для сообщения требуется существующее предложение.')
                text = text.replace('{proposal}', proposals[-1])
            sent = datetime.fromisoformat(row['sent_at'])
            msg = Message('demo', config.allowed_user, 'demo-chat', str(row['message_id']), sent, text)
            print(f'\nВы [{sent.isoformat()}] > {text}', flush=True)
            reply = service.handle(msg)
            print('Бот > ' + reply, flush=True)
            transcript.append({'message_id': msg.message_id, 'sent_at': sent.isoformat(), 'text': text, 'reply': reply})
            if args.output:
                from mealbot.storage import atomic_text
                atomic_text(Path(args.output), json.dumps(transcript, ensure_ascii=False, indent=2) + '\n')


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    try:
        main()
    except KeyboardInterrupt:
        print('\nРабота завершена. Сохранённые данные остаются в vault.')
    except (RuntimeError, ValueError, OSError) as exc:
        # Сетевые исключения адаптера предварительно очищены от URL с токеном.
        print('Ошибка: ' + str(exc), file=sys.stderr)
        sys.exit(1)
