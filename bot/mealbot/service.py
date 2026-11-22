from dataclasses import dataclass
from datetime import date, datetime, timedelta
import hashlib
import re
import uuid
from zoneinfo import ZoneInfo

from .model import MEALS, Ollama
from .storage import Vault

HELP = '''🍽 Помощник по питанию

Записываю приёмы пищи, показываю дневник и помогаю управлять графиком.

📝 Добавить запись
Просто напишите:
Сегодня на завтрак в 08:30 ел овсянку и банан

📖 Дневник и график
/today — дневник за сегодня
/yesterday — дневник за вчера
/plan — график питания

Можно спросить обычными словами:
Что я сегодня ел? Какой у меня график?

🕒 Изменить график
Например: Перенеси ужин на 19:30
/confirm ID — подтвердить изменение
/cancel ID — отменить изменение

💬 Уточнения
/answer текст — дополнить незавершённую запись
/new — закрыть уточнение и начать новую запись

В одном сообщении опишите один приём пищи и укажите день. Время и количество необязательны.

Бот ведёт дневник, но не даёт медицинские рекомендации.'''


@dataclass(frozen=True)
class Message:
    transport: str
    user_id: str
    chat_id: str
    message_id: str
    sent_at: datetime
    text: str
    private: bool = True

    @property
    def key(self):
        return f'{self.transport}:{self.chat_id}:{self.message_id}'


def day_from_text(text: str, hint: str | None, sent: datetime, frozen: str | None = None):
    # Явная числовая дата приоритетнее относительного слова и ответа LLM.
    match = re.search(r'\b(\d{4}-\d{2}-\d{2})\b', text)
    if match:
        return date.fromisoformat(match[1]).isoformat()
    match = re.search(r'\b(\d{1,2})[.](\d{1,2})(?:[.](\d{4}))?\b', text)
    if match:
        return date(int(match[3] or sent.year), int(match[2]), int(match[1])).isoformat()
    # Проверяем и текстовую дату; негрунтованная дата из LLM не принимается.
    months = ['января', 'февраля', 'марта', 'апреля', 'мая', 'июня',
              'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря']
    match = re.search(r'\b(\d{1,2})\s+(' + '|'.join(months) + r')(?:\s+(\d{4}))?\b', text.lower())
    if match:
        return date(int(match[3] or sent.year), months.index(match[2]) + 1, int(match[1])).isoformat()
    for word, delta in (('позавчера', -2), ('послезавтра', 2), ('вчера', -1), ('сегодня', 0), ('завтра', 1)):
        if re.search(r'\b' + word + r'\b', text.lower()):
            return (sent.date() + timedelta(days=delta)).isoformat()
    if frozen:
        return frozen
    return None


def clock(value):
    if value is None:
        return None
    if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', value):
        raise ValueError('Время укажите в формате ЧЧ:ММ от 00:00 до 23:59.')
    return value


class Service:
    def __init__(self, config):
        self.config = config
        self.vault = Vault(config.vault, config.allowed_user)
        self.model = Ollama(config)

    def handle(self, message: Message):
        # Важно: до блокировки, чтения vault и обращения к модели.
        if not message.private or message.user_id != self.config.allowed_user or not self.config.allowed_user:
            return 'Доступ запрещён.'
        if message.sent_at.tzinfo is None:
            raise ValueError('Время сообщения должно содержать часовой пояс.')
        if not message.text.strip() or len(message.text) > 4000:
            return 'Отправьте непустое текстовое сообщение длиной до 4000 символов.'
        with self.vault.lock():
            state = self.vault.load()
            self.vault.project(state)  # Восстановление прерванной проекции.
            if message.key in state['receipts']:
                return state['receipts'][message.key]['reply']
            sid = 'src-' + hashlib.sha256(message.key.encode()).hexdigest()[:20]
            tz = ZoneInfo(state['profile']['timezone'])
            sent = message.sent_at.astimezone(tz)
            source = {
                'source_id': sid, 'transport': message.transport, 'user_id': message.user_id,
                'chat_id': message.chat_id, 'message_id': message.message_id,
                'sent_at': sent.isoformat(), 'text': message.text,
                'recorded_at': datetime.now(tz).isoformat(timespec='seconds'),
            }
            state['sources'][sid] = source
            text = message.text.strip()
            head, _, tail = text.partition(' ')
            head = head.lower()
            if head in ('/start', '/help'):
                reply = HELP
            elif head in ('/today', '/yesterday'):
                day = sent.date() - timedelta(days=head == '/yesterday')
                reply = self.diary(state, day.isoformat())
            elif head == '/plan':
                reply = self.plan(state)
            elif head in ('/confirm', '/cancel'):
                reply = self.decide(state, sid, tail.strip(), head == '/confirm')
            elif head == '/new':
                state['pending'] = None
                reply = 'Незавершённое уточнение закрыто. Отправьте новую запись с днём и блюдами.'
            elif head.startswith('/') and head != '/answer':
                reply = 'Неизвестная команда. Используйте /help.'
            elif head == '/answer' and not state['pending']:
                reply = 'Нет незавершённого уточнения. Опишите новый приём пищи с указанием дня.'
            elif head == '/answer' and not tail.strip():
                reply = 'После /answer напишите недостающие сведения.'
            elif head != '/answer' and state['pending']:
                # Чёткая граница между новым запросом и уточнением: только /answer.
                # Чтение плана и дневника остаётся возможным в свободной форме.
                reply = self.interpret(state, message, sent, sid, text, clarification=False)
            else:
                reply = self.interpret(state, message, sent, sid, tail.strip() if head == '/answer' else text,
                                       clarification=head == '/answer')
            state['receipts'][message.key] = {'source_id': sid, 'reply': reply}
            self.vault.commit(state)
            return reply

    def interpret(self, state, message, sent, sid, text, clarification):
        pending = state['pending'] if clarification else None
        context = {
            'text': text, 'local_sent_at': sent.isoformat(),
            'timezone': state['profile']['timezone'],
            'mode': 'clarification' if clarification else 'new_request',
            'pending': pending, 'current_plan': state['plan'],
        }
        action, trace = self.model.interpret(context)
        state['sources'][sid]['trace'] = trace
        kind = action['action']
        try:
            frozen = pending['draft']['meal_date'] if pending else None
            day = day_from_text(text, action['day'], sent, frozen)
            value = clock(action['meal_time'])
        except ValueError:
            return 'Дата или время некорректны. Отправьте исправленное сообщение; факт не создан.'
        if kind == 'diary':
            return self.diary(state, day) if day else 'За какой день показать дневник? Укажите сегодня, вчера или дату.'
        if kind == 'plan':
            return self.plan(state)
        if kind == 'propose':
            return self.propose(state, sid, action['meal_type'], value)
        if kind == 'future' or (kind in ('record', 'clarify') and day and day > sent.date().isoformat()):
            return 'Это намерение на будущее. Фактический приём пищи не создан.'
        if kind == 'help':
            return HELP
        meal_mentions = {stem for stem in ('завтрак', 'обед', 'ужин', 'перекус') if re.search(r'\b' + stem + r'\w*', text.lower())}
        time_mentions = set(re.findall(r'\b\d{1,2}:\d{2}\b', text))
        sequential = re.search(r'\b(?:потом|затем|позже|дважды|два раза|через)\b', text.lower())
        one_explicit_meal = len(meal_mentions) == 1 and len(time_mentions) <= 1 and not sequential
        # Суп и хлеб на одном обеде - два продукта, а не два события.
        if len(meal_mentions) > 1 or len(time_mentions) > 1 or (action['multiple'] and not one_explicit_meal):
            return 'Опишите каждый приём пищи отдельным сообщением с указанием дня. Факты не созданы.'
        if kind not in ('record', 'clarify'):
            return 'Не удалось определить запрос. Используйте /help.'
        if not clarification and state['pending']:
            return 'Есть незавершённая запись. Ответьте /answer текст или закройте её командой /new. Новый факт не создан.'
        refs = (pending['source_refs'] if pending else []) + [sid]
        old = pending['draft'] if pending else {}
        full_text = '\n'.join(state['sources'][ref]['text'] for ref in refs)
        # Не подменять неизвестное время timestamp сообщения даже при ошибке LLM.
        reported_times = {f'{int(m[1]):02d}:{m[2]}' for m in re.finditer(r'\b(\d{1,2}):(\d{2})\b', full_text)}
        if re.search(r'\bсейчас\b', text.lower()):
            value = sent.strftime('%H:%M')
        elif value and value not in reported_times:
            value = old.get('meal_time')
        meal_type = action['meal_type']
        stem = {'breakfast': 'завтрак', 'lunch': 'обед', 'dinner': 'ужин', 'snack': 'перекус'}
        if meal_type != 'unknown' and stem[meal_type] not in full_text.lower():
            meal_type = old.get('meal_type', 'unknown')
        items = action['items'] or old.get('items', [])
        # Количество должно буквально присутствовать как отдельное выражение.
        # Цифра из 13:00 или даты не подтверждает придуманную моделью «1 порцию».
        quantity_evidence = re.sub(r'\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}[:.]\d{2}(?:[.]\d{4})?\b', '', full_text)
        quantity_evidence = re.sub(r'\b\d{1,2}\s+(?:января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)(?:\s+\d{4})?\b', '', quantity_evidence, flags=re.I)
        for item in items:
            if item['quantity'] and not re.search(r'(?<!\w)' + re.escape(item['quantity']) + r'(?!\w)', quantity_evidence, flags=re.I):
                item['quantity'] = None
        draft = {'meal_date': day, 'meal_time': value or old.get('meal_time'),
                 'meal_type': meal_type, 'items': items}
        missing = []
        if not day:
            missing.append('за какой день была еда')
        if not items:
            missing.append('какие блюда или продукты были съедены')
        if kind == 'clarify' and not missing:
            return 'Неясно, сообщаете ли вы о состоявшемся приёме пищи. Напишите день и что именно ели.'
        if missing:
            state['pending'] = {'dialogue_id': pending['dialogue_id'] if pending else uuid.uuid4().hex[:12],
                                'user_id': message.user_id, 'draft': draft, 'source_refs': refs,
                                'started_at': pending['started_at'] if pending else sent.isoformat()}
            return 'Уточните, ' + ' и '.join(missing) + '. Используйте /answer текст. Пока факт не создан.'
        eid = 'meal-' + uuid.uuid4().hex[:16]
        event = {'event_id': eid, 'user_id': message.user_id, **draft,
                 'source_refs': refs, 'recorded_at': datetime.now(ZoneInfo(state['profile']['timezone'])).isoformat(timespec='seconds')}
        state['events'][eid] = event
        if clarification:
            state['pending'] = None
        return 'Записал: ' + self.describe(event) + '.'

    @staticmethod
    def describe(event):
        foods = ', '.join(i['name'] + (f' ({i["quantity"]})' if i['quantity'] else ' (количество неизвестно)') for i in event['items'])
        return f'{event["meal_date"]}, {event["meal_time"] or "время неизвестно"}, {MEALS[event["meal_type"]]}: {foods}'

    def diary(self, state, day):
        events = sorted((e for e in state['events'].values()
                         if e['user_id'] == self.config.allowed_user and e['meal_date'] == day),
                        key=lambda e: (e['meal_time'] or '99:99', e['event_id']))
        if not events:
            return f'В дневнике за {day} записей нет. Это не означает, что вы не ели.'
        return f'Дневник за {day}:\n' + '\n'.join('• ' + self.describe(e) for e in events) + '\nДругих записей за этот день нет.'

    @staticmethod
    def plan(state):
        return 'Действующий график:\n' + '\n'.join(f'• {MEALS[m].capitalize()}: {value}' for m, value in state['plan'].items())

    def propose(self, state, sid, meal_type, value):
        if meal_type not in state['plan'] or not value:
            return 'Укажите пункт графика (завтрак, обед, ужин) и новое время ЧЧ:ММ.'
        active = next((p for p in state['proposals'].values() if p['status'] == 'pending'), None)
        if active:
            return f'Сначала завершите предложение {active["proposal_id"]}: /confirm {active["proposal_id"]} или /cancel {active["proposal_id"]}.'
        if state['plan'][meal_type] == value:
            return f'{MEALS[meal_type].capitalize()} уже запланирован на {value}.'
        pid = uuid.uuid4().hex[:12]
        state['proposals'][pid] = {
            'proposal_id': pid, 'user_id': self.config.allowed_user, 'meal_type': meal_type,
            'old_time': state['plan'][meal_type], 'new_time': value,
            'status': 'pending', 'source_refs': [sid],
        }
        return f'Предложение {pid}: {MEALS[meal_type]} с {state["plan"][meal_type]} на {value}.\nГрафик пока прежний.\nПодтвердить: /confirm {pid}\nОтменить: /cancel {pid}'

    def decide(self, state, sid, pid, confirmed):
        p = state['proposals'].get(pid)
        if not p or p['user_id'] != self.config.allowed_user:
            return 'Предложение не найдено. После команды укажите его точный ID.'
        if p['status'] != 'pending':
            return 'Это предложение уже закрыто и не может быть применено повторно.'
        if confirmed:
            if state['plan'][p['meal_type']] != p['old_time']:
                p['status'] = 'conflict'
                return 'График изменился с момента предложения. Создайте новое предложение.'
            state['plan'][p['meal_type']] = p['new_time']
        p['status'] = 'confirmed' if confirmed else 'cancelled'
        p['source_refs'].append(sid)
        return ('Изменение подтверждено.\n' + self.plan(state)) if confirmed else 'Предложение отменено. График не изменён.'
