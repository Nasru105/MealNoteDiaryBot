"""Атомарный снимок состояния и восстанавливаемые Markdown-заметки в vault."""
from contextlib import contextmanager
from datetime import datetime
import json
import os
from pathlib import Path
import tempfile
from zoneinfo import ZoneInfo

from .model import MEALS


def atomic_text(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(prefix='.' + path.name, suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(handle, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        if os.name != 'nt':
            fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def note(properties: dict, body: str) -> str:
    # JSON-значения корректны в YAML; неизвестные величины единообразно null.
    frontmatter = '\n'.join(f'{k}: {json.dumps(v, ensure_ascii=False)}' for k, v in properties.items())
    return f'---\n{frontmatter}\n---\n\n{body.strip()}\n'


def literal(text: str) -> str:
    # Исходный текст не превращается в инструкции, активные ссылки или заголовки.
    import re
    fence = '`' * max(3, max((len(m[0]) for m in re.finditer(r'`+', text)), default=0) + 1)
    return f'{fence}text\n{text}\n{fence}'


class Vault:
    def __init__(self, root: Path, user_id: str):
        self.root = root.resolve()
        self.user_id = user_id or None
        self.state_file = self.root / '.agent' / 'state.json'

    @contextmanager
    def lock(self):
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        with (self.state_file.parent / 'process.lock').open('a+b') as stream:
            stream.seek(0, 2)
            if stream.tell() == 0:
                stream.write(b'0')
                stream.flush()
            stream.seek(0)
            try:
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise RuntimeError('Это хранилище уже открыто другим процессом бота.') from None
            try:
                yield
            finally:
                stream.seek(0)
                if os.name == 'nt':
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream, fcntl.LOCK_UN)

    def load(self):
        if self.state_file.exists():
            state = json.loads(self.state_file.read_text(encoding='utf-8'))
            if state['profile']['user_id'] != self.user_id:
                # Пустой подготовленный vault можно однократно связать с владельцем.
                if state['profile']['user_id'] is None and not state['sources']:
                    state['profile']['user_id'] = self.user_id
                else:
                    raise RuntimeError('Владелец vault отличается от ALLOWED_USER_ID. Используйте отдельный vault.')
            ZoneInfo(state['profile']['timezone'])
            return state
        return {
            'schema_version': 1, 'revision': 0,
            'profile': {'name': 'student', 'user_id': self.user_id, 'timezone': 'Europe/Moscow'},
            'plan': {'breakfast': '08:30', 'lunch': '13:00', 'dinner': '19:00'},
            'events': {}, 'sources': {}, 'pending': None, 'proposals': {},
            'receipts': {}, 'telegram_offset': 0,
        }

    def commit(self, state):
        state['revision'] += 1
        atomic_text(self.state_file, json.dumps(state, ensure_ascii=False, indent=2) + '\n')
        # Ответ об успехе возможен только после завершения всех записей.
        self.project(state)

    def initialize(self):
        with self.lock():
            state = self.load()
            self.commit(state)

    def project(self, state):
        def write(relative, properties, body):
            path = self.root / relative
            content = note(properties, body)
            if not path.exists() or path.read_text(encoding='utf-8') != content:
                atomic_text(path, content)

        profile = state['profile']
        write('profile/user.md', profile,
              '# Учебный профиль\n\nИмя: student.\n\n'
              '[[index|Навигация]] · [[plans/meal-plan|График питания]]\n\n'
              'Часовой пояс хранится в профиле. Изменение конфигурации производится через state.json при остановленном боте.')
        write('plans/meal-plan.md', {'type': 'meal_plan', 'profile': '[[profile/user]]', **state['plan']},
              '# Ежедневный график\n\n' + '\n'.join(f'- {MEALS[m].capitalize()}: {t}' for m, t in state['plan'].items())
              + '\n\nПлан не является фактом приёма пищи.\n\n[[index|Навигация]]')
        for sid, source in state['sources'].items():
            write(f'sources/{sid}.md', {k: v for k, v in source.items() if k not in ('text', 'trace')},
                  '# Исходное сообщение\n\n' + literal(source['text']) + '\n\n[[profile/user|Профиль]]\n\n'
                  + ('## Интерпретация модели\n\n```json\n' + json.dumps(source['trace'], ensure_ascii=False, indent=2) + '\n```'
                     if source.get('trace') else 'Обработано командой приложения без обращения к модели.'))
        days = {}
        for eid, event in state['events'].items():
            day = event['meal_date']
            ref = f'events/{day}/{eid}'
            days.setdefault(day, []).append(ref)
            props = {k: v for k, v in event.items() if k != 'items'}
            props['profile'] = '[[profile/user]]'
            props['source_refs'] = [f'[[sources/{s}]]' for s in event['source_refs']]
            body = f'# {MEALS[event["meal_type"]].capitalize()} за {day}\n\n'
            body += f'Время: {event["meal_time"] or "неизвестно"}.\n\n'
            body += '\n'.join(f'- {i["name"]}: {i["quantity"] or "количество неизвестно"}' for i in event['items'])
            body += '\n\nКалорийность: неизвестно.\n\n## Источники\n\n'
            body += '\n'.join(f'- [[sources/{s}]]' for s in event['source_refs'])
            body += f'\n\n[[profile/user|Профиль]] · [[summaries/{day}|Дневная сводка]] · [[index|Навигация]]'
            write(ref + '.md', props, body)
        for day, refs in days.items():
            write(f'summaries/{day}.md', {'type': 'daily_summary', 'meal_date': day},
                  f'# Дневник за {day}\n\n' + '\n'.join(f'- [[{r}]]' for r in refs)
                  + '\n\nСводка производная: записи находятся в events.\n\n[[index|Навигация]]')
        for pid, proposal in state['proposals'].items():
            write(f'proposals/{pid}.md', proposal,
                  f'# Предложение {pid}\n\n{MEALS[proposal["meal_type"]]}: {proposal["old_time"]} → {proposal["new_time"]}.\n\n'
                  '[[plans/meal-plan|Действующий график]]\n\n'
                  + '\n'.join(f'[[sources/{s}]]' for s in proposal['source_refs']))
        pending = state['pending']
        write('dialogues/pending.md', {'type': 'pending_dialogue', 'active': pending is not None},
              '# Незавершённое уточнение\n\n' +
              ('```json\n' + json.dumps(pending, ensure_ascii=False, indent=2) + '\n```\n\n'
               + '\n'.join(f'[[sources/{s}]]' for s in pending['source_refs']) if pending else 'Нет незавершённых уточнений.'))
        write('rules.md', {'type': 'rules'}, '''# Правила ведения базы

- График, фактические события и исходные сообщения разделены.
- Неизвестные значения хранятся как null, в тексте показаны как «неизвестно».
- Дата и время еды отличаются от даты отправки и времени сохранения.
- Модель извлекает только явно сообщённые сведения и не имеет доступа к файлам.
- Состояние приложения: .agent/state.json внутри этого хранилища.
- Markdown является автоматически восстанавливаемой проекцией состояния.
- Ручные правки Markdown будут перезаписаны; меняйте данные через приложение.
- Перед резервным копированием остановите бота и скопируйте весь vault вместе с .agent.
- Уточнение отправляется командой /answer; новое сообщение - отдельный запрос.
- Изменение плана применяется только командой /confirm с идентификатором предложения.

[[index|Навигация]]''')
        write('index.md', {'type': 'index', 'revision': state['revision']},
              '# База знаний помощника\n\n[[profile/user|Профиль student]] · [[plans/meal-plan|График питания]]\n\n'
              '[[rules|Правила]] · [[dialogues/pending|Незавершённый диалог]]\n\n## Дневник\n\n'
              + ('\n'.join(f'- [[summaries/{day}|{day}]]' for day in sorted(days, reverse=True)) or 'Событий пока нет.')
              + '\n\n## Все события\n\n' + '\n'.join(f'- [[{r}]]' for refs in days.values() for r in refs)
              + '\n\n## Предложения изменения графика\n\n'
              + ('\n'.join(f'- [[proposals/{p}]]' for p in state['proposals']) or 'Предложений пока нет.'))
