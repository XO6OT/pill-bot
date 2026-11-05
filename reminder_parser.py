"""Разбор фраз вида «напомни мне в 12:30 о звонке» в напоминание."""
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

TRIGGER = re.compile(r"напом\w*", re.IGNORECASE)

# «напомни», «напомни мне», «напомните пожалуйста» — убираем из текста напоминания
TRIGGER_PHRASE = re.compile(
    r"\b(?:пожалуйста\s+)?напом\w*(?:\s+(?:мне|меня|нам|пожалуйста))*\b", re.IGNORECASE
)
DAILY = re.compile(r"\b(?:каждый\s+день|ежедневно)\b", re.IGNORECASE)
RELATIVE = re.compile(
    r"\bчерез\s+(?:(\d+)\s*)?(минут[уы]?|мин|час(?:а|ов)?|ч)\b(?:\s+(\d+)\s*(?:минут[уы]?|мин))?",
    re.IGNORECASE,
)
HALF_HOUR = re.compile(r"\bчерез\s+полчаса\b", re.IGNORECASE)
DAY_WORD = re.compile(r"\b(сегодня|завтра|послезавтра)\b", re.IGNORECASE)
TIME_HM = re.compile(r"(?:\b(?:в|на|к)\s+)?\b([01]?\d|2[0-3])\s*[:.]\s*([0-5]\d)\b", re.IGNORECASE)
TIME_H = re.compile(
    r"\b(?:в|на|к)\s+([01]?\d|2[0-3])(?:\s*(?:час(?:а|ов)?|ч)\b)?"
    r"(?:\s+(утра|дня|вечера|ночи))?\b",
    re.IGNORECASE,
)
PART_OF_DAY = re.compile(r"^\s*(утра|дня|вечера|ночи)\b", re.IGNORECASE)
LEADING_WORDS = re.compile(r"^(?:(?:что\s+надо|чтобы|что)\s+)", re.IGNORECASE)
PREPOSITION = re.compile(r"^(?:о|об|обо|про|насчёт|насчет)\s", re.IGNORECASE)

DAY_OFFSET = {"сегодня": 0, "завтра": 1, "послезавтра": 2}


@dataclass
class ParsedReminder:
    remind_at: datetime
    text: str
    daily: bool


class ReminderError(ValueError):
    """Понятное пользователю объяснение, почему напоминание не создано."""


def looks_like_reminder(text):
    return bool(text) and bool(TRIGGER.search(text))


def _apply_part_of_day(hour, part):
    part = (part or "").lower()
    if part in ("дня", "вечера") and hour < 12:
        return hour + 12
    if part == "ночи" and hour == 12:
        return 0
    return hour


def parse_reminder(text, now):
    """Возвращает ParsedReminder или бросает ReminderError.

    now — текущее время с часовым поясом пользователя.
    """
    cut = []  # куски фразы, которые относятся ко времени, а не к тексту

    daily = DAILY.search(text)
    if daily:
        cut.append(daily.span())

    relative = RELATIVE.search(text)
    half_hour = None if relative else HALF_HOUR.search(text)
    hm = TIME_HM.search(text)
    h = None if hm else TIME_H.search(text)
    day = DAY_WORD.search(text)

    if half_hour:
        remind_at = now.replace(second=0, microsecond=0) + timedelta(minutes=30)
        cut.append(half_hour.span())
    elif relative:
        amount = int(relative.group(1) or 1)
        unit = relative.group(2).lower()
        minutes = amount * 60 if unit.startswith("ч") else amount
        if relative.group(3):
            minutes += int(relative.group(3))
        if minutes <= 0:
            raise ReminderError("Через сколько напомнить? Например: «через 20 минут напомни выпить воды».")
        remind_at = now.replace(second=0, microsecond=0) + timedelta(minutes=minutes)
        cut.append(relative.span())
    elif hm or h:
        if hm:
            hour, minute = int(hm.group(1)), int(hm.group(2))
            cut.append(hm.span())
            part = PART_OF_DAY.match(text[hm.end():])
            if part:
                hour = _apply_part_of_day(hour, part.group(1))
                cut.append((hm.end(), hm.end() + part.end()))
        else:
            hour, minute = _apply_part_of_day(int(h.group(1)), h.group(2)), 0
            cut.append(h.span())

        offset = DAY_OFFSET[day.group(1).lower()] if day else 0
        if day:
            cut.append(day.span())
        remind_at = now.replace(hour=hour, minute=minute, second=0, microsecond=0) + timedelta(days=offset)
        if remind_at <= now:
            if day and offset == 0 and not daily:
                raise ReminderError(f"Сегодня {hour:02d}:{minute:02d} уже прошло. Напиши время позже или «завтра».")
            if not day or daily:
                remind_at += timedelta(days=1)
    else:
        raise ReminderError(
            "Не получилось понять время 🙈 Напиши, например:\n"
            "• напомни мне в 12:30 о звонке\n"
            "• в 18:00 напомни купить хлеб\n"
            "• завтра в 9 напомни про врача\n"
            "• через 20 минут напомни выпить воды\n"
            "• каждый день в 8:00 напомни про витамины"
        )

    if daily and (relative or half_hour):
        raise ReminderError("Для «каждый день» нужно точное время, например «каждый день в 8:00».")

    rest = text
    for start, end in sorted(cut, reverse=True):
        rest = rest[:start] + " " + rest[end:]
    rest = TRIGGER_PHRASE.sub(" ", rest)
    rest = re.sub(r"\s+", " ", rest).strip(" ,.:;-—!")
    rest = LEADING_WORDS.sub("", rest).strip(" ,.:;-—!")

    if not rest:
        raise ReminderError("О чём напомнить? Например: «напомни мне в 12:30 о звонке маме».")

    if PREPOSITION.match(rest):
        rest = rest[0].lower() + rest[1:]
    else:
        rest = rest[0].upper() + rest[1:]
    return ParsedReminder(remind_at=remind_at, text=rest, daily=bool(daily))


def with_reminder_text(prefix, text):
    """«Напомню» + «о звонке» или «Напомню» + «: купить хлеб»."""
    return f"{prefix} {text}" if PREPOSITION.match(text) else f"{prefix}: {text}"
