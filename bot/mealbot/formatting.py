"""Представление ответов в Telegram MarkdownV2 без разметки в данных базы."""
from dataclasses import dataclass
from datetime import date
import re


@dataclass(frozen=True)
class Span:
    text: str
    style: str = ''


RESERVED = frozenset('\\_*[]()~`>#+-=|{}.!')
HEADINGS = frozenset({
    '🍽 Помощник по питанию', '📝 Добавить запись', '📖 Дневник и график',
    '🕒 Изменить график', '💬 Уточнения',
})
COMMAND = re.compile(
    r'(?<!\w)/(?:today|yesterday|plan|confirm|cancel|answer|new|help|start)\b'
    r'(?: (?:[0-9a-f]{12}\b|ID\b|текст\b))?'
)
EVENT = re.compile(r'(\d{4}-\d{2}-\d{2}), ([^,\n]+), ([^:\n]+): (.+)', re.S)
MONTHS = ('января', 'февраля', 'марта', 'апреля', 'мая', 'июня',
          'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря')


def display_date(value):
    day = date.fromisoformat(value)
    return f'{day.day} {MONTHS[day.month - 1]} {day.year}'


def inline_commands(text):
    """Разметка создаётся приложением; произвольный текст всегда экранируется."""
    spans = []
    start = 0
    for match in COMMAND.finditer(text):
        spans.extend((Span(text[start:match.start()]), Span(match[0], 'code')))
        start = match.end()
    spans.append(Span(text[start:]))
    return spans


def event_spans(value, show_date=False):
    match = EVENT.fullmatch(value)
    if not match:
        return [Span(value)]
    day, meal_time, meal_type, foods = match.groups()
    spans = []
    if show_date:
        spans.extend((Span('📅 ' + display_date(day)), Span('\n')))
    spans.extend((Span(meal_type.capitalize() + ' · ' + meal_time, 'bold'),
                  Span('\n' + foods)))
    return spans


def reply_spans(text):
    if text.startswith('🍽 Помощник по питанию\n'):
        spans = []
        for line in text.splitlines(keepends=True):
            value = line.rstrip('\n')
            if value in HEADINGS:
                spans.append(Span(value, 'bold'))
            elif value.startswith('Бот ведёт дневник, но '):
                spans.append(Span(value, 'italic'))
            elif value == 'Сегодня на завтрак в 08:30 ел овсянку и банан':
                spans.append(Span(value, 'code'))
            else:
                spans.extend(inline_commands(value))
            if line.endswith('\n'):
                spans.append(Span('\n'))
        return spans

    if text.startswith('Изменение подтверждено.\n'):
        return [Span('✅ График обновлён', 'bold'), Span('\n\n')] + reply_spans(text.split('\n', 1)[1])

    if text.startswith('Действующий график:\n'):
        spans = [Span('🕒 График питания', 'bold'), Span('\n\n')]
        for line in text.splitlines()[1:]:
            match = re.fullmatch(r'• ([^:\n]+): (\d{2}:\d{2})', line)
            if match:
                spans.extend((Span('• ' + match[1] + ' — '), Span(match[2], 'code'), Span('\n')))
            else:
                spans.append(Span(line + '\n'))
        return spans

    diary = re.match(r'Дневник за (\d{4}-\d{2}-\d{2}):\n', text)
    if diary:
        spans = [Span('📖 Дневник за ' + display_date(diary[1]), 'bold')]
        for line in text[diary.end():].splitlines():
            spans.append(Span('\n\n'))
            if line.startswith('• '):
                spans.extend(event_spans(line[2:]))
            else:
                spans.append(Span(line, 'italic'))
        return spans

    empty = re.fullmatch(r'В дневнике за (\d{4}-\d{2}-\d{2}) записей нет\. Это не означает, что вы не ели\.', text)
    if empty:
        return [Span('📖 Дневник за ' + display_date(empty[1]), 'bold'),
                Span('\n\nЗаписей пока нет.\n\n'),
                Span('Отсутствие записей не означает, что вы не ели.', 'italic')]

    if text.startswith('Записал: '):
        value = text[len('Записал: '):]
        if value.endswith('.'):
            value = value[:-1]
        return [Span('✅ Приём пищи сохранён', 'bold'), Span('\n\n')] + event_spans(value, show_date=True)

    proposal = re.fullmatch(
        r'Предложение ([0-9a-f]{12}): ([^\n]+) с (\d{2}:\d{2}) на (\d{2}:\d{2})\.'
        r'\nГрафик пока прежний\.\nПодтвердить: /confirm \1\nОтменить: /cancel \1', text)
    if proposal:
        pid, meal, old, new = proposal.groups()
        return [Span('🕒 Изменение графика', 'bold'), Span('\n\n' + meal.capitalize() + ': '),
                Span(old, 'code'), Span(' → '), Span(new, 'code'),
                Span('\n\nГрафик изменится только после подтверждения.\n\n'),
                Span('✅ Подтвердить', 'bold'), Span('\n'), Span('/confirm ' + pid, 'code'),
                Span('\n\n'), Span('↩️ Отменить', 'bold'), Span('\n'), Span('/cancel ' + pid, 'code')]

    if text == 'Предложение отменено. График не изменён.':
        return [Span('↩️ Изменение отменено', 'bold'), Span('\n\nГрафик остался прежним.')]
    return inline_commands(text)


def utf16_length(value):
    return len(value.encode('utf-16-le')) // 2


def render_reply(text, limit=3500):
    """Делит длинный ответ, закрывая разметку до границы каждого сообщения.

    Ограничивается даже длина экранированного текста в UTF-16: это оставляет
    запас относительно 4096 символов Telegram после разбора сущностей.
    Экранированный символ и суррогатная пара никогда не разрываются.
    """
    markers = {'': '', 'bold': '*', 'italic': '_', 'code': '`'}
    messages = []
    current = ''
    current_size = 0
    for span in reply_spans(text):
        marker = markers[span.style]
        marker_size = len(marker) * 2
        buffer = []
        buffer_size = 0
        for char in span.text:
            reserved = char in ('\\', '`') if span.style == 'code' else char in RESERVED
            escaped = ('\\' if reserved else '') + char
            size = utf16_length(escaped)
            if current_size + marker_size + buffer_size + size > limit:
                if buffer:
                    current += marker + ''.join(buffer) + marker
                if current.strip():
                    messages.append(current)
                current, current_size = '', 0
                buffer, buffer_size = [], 0
            buffer.append(escaped)
            buffer_size += size
        if buffer:
            current += marker + ''.join(buffer) + marker
            current_size += buffer_size + marker_size
    if current.strip():
        messages.append(current)
    return messages
