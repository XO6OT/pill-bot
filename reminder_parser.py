"""Разбор фраз вида «напомни мне в 12:30 о звонке» в напоминание.

Понимает время цифрами и словами, «через …», дни недели, даты,
«утром/вечером», а также повторы «каждый день» и «каждый понедельник».
"""
import re
from dataclasses import dataclass
from datetime import timedelta

I = re.IGNORECASE

# --- КАК ПРОСЯТ НАПОМНИТЬ ---

ASK = (
    r"напом\w*"
    r"|не\s+дай\s+(?:мне\s+)?забыть"
    r"|(?:поставь|сделай|создай|заведи)\s+(?:мне\s+)?(?:будильник|напоминани\w*)"
)
TRIGGER = re.compile(ASK, I)
TRIGGER_PHRASE = re.compile(
    rf"(?:\b(?:пожалуйста|плиз|плз)\s+)?(?:{ASK})(?:\s+(?:мне|меня|нам|пожалуйста|плиз|плз))*\b", I
)

# --- ЧИСЛА СЛОВАМИ ---

UNITS = {
    "ноль": 0, "один": 1, "одна": 1, "одну": 1, "два": 2, "две": 2, "три": 3, "четыре": 4,
    "пять": 5, "шесть": 6, "семь": 7, "восемь": 8, "девять": 9, "десять": 10,
    "одиннадцать": 11, "двенадцать": 12, "тринадцать": 13, "четырнадцать": 14,
    "пятнадцать": 15, "шестнадцать": 16, "семнадцать": 17, "восемнадцать": 18,
    "девятнадцать": 19,
}
TENS = {"двадцать": 20, "тридцать": 30, "сорок": 40, "пятьдесят": 50}
NUM_WORD = "|".join(sorted([*UNITS, *TENS], key=len, reverse=True))
NUM_SEQ = rf"(?:{NUM_WORD})(?:\s+(?:{NUM_WORD})){{0,3}}"

# «без пятнадцати девять», «в половине девятого»
WITHOUT_WORDS = {"четверти": 15, "пяти": 5, "десяти": 10, "пятнадцати": 15, "двадцати пяти": 25, "двадцати": 20}
ORDINALS = {
    "первого": 1, "второго": 2, "третьего": 3, "четвёртого": 4, "четвертого": 4, "пятого": 5,
    "шестого": 6, "седьмого": 7, "восьмого": 8, "девятого": 9, "десятого": 10,
    "одиннадцатого": 11, "двенадцатого": 12,
}


def _words_to_numbers(words):
    """«девять тридцать» → [9, 30], «двадцать один сорок пять» → [21, 45]."""
    numbers, after_tens = [], False
    for word in words.lower().split():
        value = UNITS.get(word, TENS.get(word))
        if after_tens and value < 10:
            numbers[-1] += value
            after_tens = False
        else:
            numbers.append(value)
            after_tens = word in TENS
    return numbers


def _number(token):
    token = token.lower().strip()
    if token.isdigit():
        return int(token)
    if token in WITHOUT_WORDS:
        return WITHOUT_WORDS[token]
    return sum(_words_to_numbers(token))


NUM_AFTER_PREP = re.compile(rf"\b(в|во|к|на|около|через)\s+({NUM_SEQ})\b", I)
NUM_BEFORE_UNIT = re.compile(rf"\b({NUM_SEQ})(?=\s+(?:минут|мин\b|час|дн|день|сут|недел))", I)


def _digits_for_words(text):
    """Числа словами рядом со временем превращает в цифры: «в девять тридцать» → «в 9:30»."""
    def after_prep(match):
        prep, numbers = match.group(1), _words_to_numbers(match.group(2))
        if prep.lower() == "через" or len(numbers) == 1:
            return f"{prep} {numbers[0]}" + "".join(f" {n}" for n in numbers[1:])
        if len(numbers) == 2 and numbers[0] <= 23 and numbers[1] <= 59:
            return f"{prep} {numbers[0]}:{numbers[1]:02d}"
        return match.group(0)

    text = NUM_AFTER_PREP.sub(after_prep, text)
    return NUM_BEFORE_UNIT.sub(lambda m: str(sum(_words_to_numbers(m.group(1)))), text)


# --- ПОВТОРЫ ---

DAILY = re.compile(r"\b(?:каждый\s+день|ежедневно|каждое\s+(утро)|каждый\s+(вечер)|каждую\s+(ночь)|по\s+(утрам|вечерам))\b", I)
WEEKDAY_FORMS = (
    r"понедельник(?:ам)?|вторник(?:ам)?|сред[ауы]|средам|четверг(?:ам)?|пятниц[ауы]|пятницам"
    r"|суббот[ауы]|субботам|воскресень[еяю]|воскресеньям"
)
WEEKDAY = re.compile(rf"\b(?:(каждый|каждую|каждое|по)\s+|(?:в|во)\s+)?({WEEKDAY_FORMS})\b", I)
WEEKDAY_INDEX = {"пон": 0, "вто": 1, "сре": 2, "чет": 3, "пят": 4, "суб": 5, "вос": 6}

# --- «ЧЕРЕЗ …» ---

RELATIVE = re.compile(
    r"\bчерез\s+(?:(\d+|пару|пара|несколько)\s*)?"
    r"(полчаса|полтора\s+часа|минут\w*|мин\b|м\b|час\w*|ч\b|дн\w*|день|сут\w*|недел\w*)"
    r"(?:\s+(\d+)\s*(?:минут\w*|мин\b|м\b))?",
    I,
)

# --- ДАТЫ И ДНИ ---

MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6, "июля": 7,
    "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}
DATE_WORDS = re.compile(rf"\b(\d{{1,2}})\s+({'|'.join(MONTHS)})(?:\s+(\d{{4}}))?(?:\s*г(?:ода|\.)?)?\b", I)
DATE_NUM = re.compile(r"\b(\d{1,2})\.(\d{1,2})(?:\.(\d{2}|\d{4}))?\b")
DAY_WORD = re.compile(r"\b(сегодня|завтра|послезавтра)\b", I)
DAY_OFFSET = {"сегодня": 0, "завтра": 1, "послезавтра": 2}

# --- ВРЕМЯ ---

NOON = re.compile(r"\b(?:в\s+|к\s+)?(полдень|полночь)\b", I)
HALF_PAST = re.compile(rf"\b(?:в\s+|к\s+)?(?:половин[еуа]\s+|пол\s*)({'|'.join(ORDINALS)})\b", I)
WITHOUT = re.compile(
    rf"\b(?:в\s+|к\s+)?без\s+({'|'.join(WITHOUT_WORDS)}|\d{{1,2}})\s*(?:минут\w*\s+)?(\d{{1,2}}|{NUM_SEQ})\b", I
)
ONE_O_CLOCK = re.compile(r"\b(?:в|к|около)\s+час\b(?!\s*[:.]?\s*\d)", I)
TIME_HM = re.compile(r"(?:\b(?:в|во|на|к|около)\s+)?\b([01]?\d|2[0-3])\s*[:.]\s*([0-5]\d)\b", I)
TIME_H_SPACE_M = re.compile(
    r"\b(?:в|во|на|к|около)\s+([01]?\d|2[0-3])[\s\-]([0-5]\d)\b(?!\s*(?:минут|мин|час|ч\b|раз|дн))", I
)
TIME_H = re.compile(r"\b(?:в|во|на|к|около)\s+([01]?\d|2[0-3])(?:\s*(?:час\w*|ч)\b)?(?!\s*(?:минут|мин|дней|раз))", I)

PART_AFTER_TIME = re.compile(r"^\s*(утра|дня|вечера|ночи)\b", I)
PART_STANDALONE = re.compile(r"\b(?:с\s+)?(утром|утра|днём|днем|вечером|вечера|ночью|ночи)\b", I)
DEFAULT_TIME = {"утро": (9, 0), "день": (13, 0), "вечер": (19, 0), "ночь": (23, 0)}

# --- ТЕКСТ НАПОМИНАНИЯ ---

# «Котик, напомни …» — обращение в начале не часть напоминания
ADDRESS = re.compile(
    r"^\s*(?:котик|котёнок|котенок|зай|зайка|зайчик|солнце|солнышко|малыш|любимый|любимая|милый|милая"
    r"|дорогой|дорогая|бот|ботик|напоминалка)\s*[,!]\s*",
    I,
)
LEADING_WORDS = re.compile(
    r"^(?:(?:что\s+(?:мне\s+)?(?:надо|нужно)|чтобы\s+(?:я\s+)?не\s+забыть|чтобы\s+я|чтобы|что|и)\s+)", I
)
TRAILING_WORDS = re.compile(r"\s+(?:в|во|на|к|и|а|около)$", I)
PREPOSITION = re.compile(r"^(?:о|об|обо|про|насчёт|насчет)\s", I)

HELP_EXAMPLES = (
    "• напомни мне в 12:30 о звонке\n"
    "• в шесть вечера напомни купить хлеб\n"
    "• завтра утром напомни про врача\n"
    "• в пятницу в 10 напомни оплатить интернет\n"
    "• 20 ноября в 12:00 напомни про день рождения\n"
    "• через полчаса напомни выпить воды\n"
    "• каждый день в 8:00 напомни про витамины\n"
    "• каждый понедельник в 10 напоминай про отчёт"
)
HELP = "Не получилось понять время 🙈 Напиши, например:\n" + HELP_EXAMPLES


@dataclass
class ParsedReminder:
    remind_at: object  # datetime с часовым поясом
    text: str
    repeat_days: int = 0  # 0 — один раз, 1 — каждый день, 7 — каждую неделю


class ReminderError(ValueError):
    """Понятное пользователю объяснение, почему напоминание не создано."""


def looks_like_reminder(text):
    return bool(text) and bool(TRIGGER.search(text))


def _part_of_day(word):
    word = (word or "").lower()
    for stem, part in (("утр", "утро"), ("дн", "день"), ("веч", "вечер"), ("ноч", "ночь")):
        if word.startswith(stem):
            return part
    return None


def _apply_part(hour, part):
    if part == "день" and hour <= 6:
        return hour + 12
    if part == "вечер" and hour < 12:
        return hour + 12
    if part == "ночь":
        if hour == 12:
            return 0
        if 6 <= hour < 12:
            return hour + 12
    return hour


class _Phrase:
    """Текст, из которого по одному вырезаются куски про время."""

    def __init__(self, text):
        self.text = text

    def take(self, pattern):
        match = pattern.search(self.text)
        if match:
            self.blank(match.start(), match.end())
        return match

    def blank(self, start, end):
        self.text = self.text[:start] + " " * (end - start) + self.text[end:]


def _find_time(phrase):
    """Ищет время суток. Возвращает (час, минута, слово про часть дня рядом) или None."""
    if match := phrase.take(NOON):
        return (12, 0, None) if match.group(1).lower() == "полдень" else (0, 0, None)
    if match := phrase.take(HALF_PAST):
        hour = ORDINALS[match.group(1).lower()] - 1
        return (hour or 12, 30, None)
    if match := phrase.take(WITHOUT):
        minutes, hour = _number(match.group(1)), _number(match.group(2))
        if not 0 < minutes < 60 or not 1 <= hour <= 24:
            return None
        return ((hour - 1) % 24, 60 - minutes, None)
    for pattern in (TIME_HM, TIME_H_SPACE_M, TIME_H, ONE_O_CLOCK):
        match = pattern.search(phrase.text)
        if not match:
            continue
        if pattern is ONE_O_CLOCK:
            hour, minute = 1, 0
        else:
            hour = int(match.group(1))
            minute = int(match.group(2)) if pattern is not TIME_H else 0
        phrase.blank(match.start(), match.end())
        part = PART_AFTER_TIME.match(phrase.text[match.end():])
        if part:
            phrase.blank(match.end(), match.end() + part.end())
            return (hour, minute, part.group(1))
        if pattern is ONE_O_CLOCK:
            hour = 13  # «в час» без уточнения — днём
        return (hour, minute, None)
    return None


def _find_date(phrase, now):
    """Дата «5 ноября» или «12.11». Число через точку без года считаем датой,
    только если во фразе есть ещё и время: иначе «напомни 10.30» — это время."""
    if match := phrase.take(DATE_WORDS):
        return _make_date(now, int(match.group(1)), MONTHS[match.group(2).lower()], match.group(3))

    for match in DATE_NUM.finditer(phrase.text):
        before = phrase.text[:match.start()].rstrip().lower()
        if before.endswith((" в", " во", " к", " на", " около")) or before in ("в", "во", "к", "на", "около"):
            continue
        day, month, year = int(match.group(1)), int(match.group(2)), match.group(3)
        if not (1 <= day <= 31 and 1 <= month <= 12):
            continue
        probe = _Phrase(phrase.text[:match.start()] + " " * (match.end() - match.start()) + phrase.text[match.end():])
        if year or _find_time(probe):
            phrase.blank(match.start(), match.end())
            return _make_date(now, day, month, year)
    return None


def _make_date(now, day, month, year):
    if year and len(year) == 2:
        year = "20" + year
    try:
        date = now.date().replace(year=int(year) if year else now.year, month=month, day=day)
    except ValueError:
        raise ReminderError("Такой даты нет 🙈 Проверь число и месяц.")
    if not year and date < now.date():
        date = date.replace(year=date.year + 1)
    return date, bool(year)


def parse_reminder(text, now):
    """Возвращает ParsedReminder или бросает ReminderError.

    now — текущее время с часовым поясом пользователя.
    """
    phrase = _Phrase(_digits_for_words(text))
    now0 = now.replace(second=0, microsecond=0)

    repeat_days, part = 0, None
    if match := phrase.take(DAILY):
        repeat_days = 1
        part = _part_of_day(next((g for g in match.groups() if g), None))

    weekday = None
    if match := phrase.take(WEEKDAY):
        weekday = WEEKDAY_INDEX[match.group(2).lower()[:3]]
        if match.group(1) or match.group(2).lower().endswith(("ам", "ям")):
            repeat_days = 7

    relative = phrase.take(RELATIVE)
    explicit_date = None if relative else _find_date(phrase, now)
    day_word = phrase.take(DAY_WORD)
    time = _find_time(phrase)
    if match := phrase.take(PART_STANDALONE):
        part = part or _part_of_day(match.group(1))
    if time and time[2]:
        part = _part_of_day(time[2])

    if relative and repeat_days:
        raise ReminderError("Для повтора нужно точное время, например «каждый день в 8:00».")

    if relative:
        amount_word = (relative.group(1) or "").lower()
        amount = {"": 1, "пару": 2, "пара": 2, "несколько": 5}.get(amount_word) or int(amount_word)
        unit = relative.group(2).lower()
        if unit.startswith("полч"):
            minutes, days = 30, 0
        elif unit.startswith("полт"):
            minutes, days = 90, 0
        elif unit.startswith("м"):
            minutes, days = amount, 0
        elif unit.startswith("ч"):
            minutes, days = amount * 60, 0
        elif unit.startswith("нед"):
            minutes, days = 0, amount * 7
        else:
            minutes, days = 0, amount
        if relative.group(3):
            minutes += int(relative.group(3))
        if minutes + days <= 0:
            raise ReminderError("Через сколько напомнить? Например: «через 20 минут напомни выпить воды».")

        if minutes:
            remind_at = now0 + timedelta(minutes=minutes)
        else:
            remind_at = now0 + timedelta(days=days)
            if time or part:
                hour, minute = (time[0], time[1]) if time else DEFAULT_TIME[part]
                remind_at = remind_at.replace(hour=_apply_part(hour, part), minute=minute)
    else:
        if time:
            hour, minute = _apply_part(time[0], part), time[1]
        elif part:
            hour, minute = DEFAULT_TIME[part]
        elif explicit_date or weekday is not None or day_word:
            hour, minute = 9, 0  # «завтра напомни …» — с утра
        else:
            raise ReminderError(HELP)

        if explicit_date:
            date, has_year = explicit_date
            remind_at = now0.replace(year=date.year, month=date.month, day=date.day, hour=hour, minute=minute)
            if remind_at <= now:
                raise ReminderError("Это время уже прошло 🙈 Напиши дату или время позже.")
        elif weekday is not None:
            days_ahead = (weekday - now.weekday()) % 7
            remind_at = now0.replace(hour=hour, minute=minute) + timedelta(days=days_ahead)
            if remind_at <= now:
                remind_at += timedelta(days=7)
        else:
            offset = DAY_OFFSET[day_word.group(1).lower()] if day_word else 0
            remind_at = now0.replace(hour=hour, minute=minute) + timedelta(days=offset)
            if remind_at <= now:
                if day_word and offset == 0 and not repeat_days:
                    raise ReminderError(
                        f"Сегодня {hour:02d}:{minute:02d} уже прошло. Напиши время позже или «завтра»."
                    )
                remind_at += timedelta(days=1)

    rest = TRIGGER_PHRASE.sub(" ", ADDRESS.sub("", phrase.text))
    rest = re.sub(r"\s+", " ", rest).strip(" ,.:;-—!?")
    for _ in range(2):
        rest = LEADING_WORDS.sub("", rest).strip(" ,.:;-—!?")
        rest = TRAILING_WORDS.sub("", rest).strip(" ,.:;-—!?")

    if not rest:
        raise ReminderError("О чём напомнить? Например: «напомни мне в 12:30 о звонке маме».")

    if rest.isupper():
        rest = rest.lower()
    if PREPOSITION.match(rest):
        rest = rest[0].lower() + rest[1:]
    else:
        rest = rest[0].upper() + rest[1:]
    return ParsedReminder(remind_at=remind_at, text=rest, repeat_days=repeat_days)


def with_reminder_text(prefix, text):
    """«Напомню» + «о звонке» или «Напомню» + «: купить хлеб»."""
    return f"{prefix} {text}" if PREPOSITION.match(text) else f"{prefix}: {text}"


EVERY_WEEKDAY = [
    "каждый понедельник", "каждый вторник", "каждую среду", "каждый четверг",
    "каждую пятницу", "каждую субботу", "каждое воскресенье",
]
SHORT_WEEKDAY = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


def describe_when(remind_at, repeat_days, now):
    """«сегодня в 12:30», «завтра в 09:00», «07.11 (пт) в 10:00», «каждый день в 08:00»."""
    local = remind_at.astimezone(now.tzinfo)
    clock = local.strftime("%H:%M")
    if repeat_days == 1:
        return f"каждый день в {clock}"
    if repeat_days == 7:
        return f"{EVERY_WEEKDAY[local.weekday()]} в {clock}"
    days = (local.date() - now.date()).days
    if days == 0:
        return f"сегодня в {clock}"
    if days == 1:
        return f"завтра в {clock}"
    return f"{local.strftime('%d.%m')} ({SHORT_WEEKDAY[local.weekday()]}) в {clock}"
