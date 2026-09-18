"""Small explicit scheduling grammar; local times are never silently corrected."""

import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from .models import DEFAULT_TIMEZONE, ReminderError, Schedule

_UNITS = {
    "s": 1,
    "sec": 1,
    "second": 1,
    "seconds": 1,
    "с": 1,
    "сек": 1,
    "секунд": 1,
    "секунду": 1,
    "секунды": 1,
    "m": 60,
    "min": 60,
    "minute": 60,
    "minutes": 60,
    "м": 60,
    "мин": 60,
    "минут": 60,
    "минуту": 60,
    "минуты": 60,
    "h": 3600,
    "hour": 3600,
    "hours": 3600,
    "ч": 3600,
    "час": 3600,
    "часа": 3600,
    "часов": 3600,
    "d": 86400,
    "day": 86400,
    "days": 86400,
    "д": 86400,
    "день": 86400,
    "дня": 86400,
    "дней": 86400,
    "w": 604800,
    "week": 604800,
    "weeks": 604800,
    "н": 604800,
    "неделю": 604800,
    "недели": 604800,
    "недель": 604800,
}
_DURATION = re.compile(r"\s*(\d{1,9})\s*(" + "|".join(sorted(_UNITS, key=len, reverse=True)) + r")(?![A-Za-zА-Яа-я])", re.IGNORECASE)
_ABSOLUTE = re.compile(r"(?:at\s+|в\s+)?(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})(?:\s+\[([^\]]+)\])?(?:\s+|$)", re.IGNORECASE)


def parse_schedule(text: str, now: datetime, default_timezone: str = DEFAULT_TIMEZONE) -> Schedule:
    """Parse EN/RU durations or ISO local date/time with an optional [IANA zone]."""
    text = text.strip()
    try:
        zone = ZoneInfo(default_timezone)
        if relative := re.match(r"(?:in|через)\s+", text, re.IGNORECASE):
            cursor, seconds, count = relative.end(), 0, 0
            while unit := _DURATION.match(text, cursor):
                seconds += int(unit[1]) * _UNITS[unit[2].casefold()]
                count += 1
                cursor = unit.end()
            if not count or seconds <= 0:
                raise ReminderError("Укажи интервал: например, через 2ч 30м или in 15m.")
            due = now.astimezone(UTC) + timedelta(seconds=seconds)
        elif absolute := _ABSOLUTE.match(text):
            zone = ZoneInfo(absolute[3] or default_timezone)
            local = datetime.fromisoformat(f"{absolute[1]}T{absolute[2]}")
            candidates = {
                local.replace(tzinfo=zone, fold=fold).astimezone(UTC)
                for fold in (0, 1)
                if local.replace(tzinfo=zone, fold=fold).astimezone(UTC).astimezone(zone).replace(tzinfo=None) == local
            }
            if len(candidates) != 1:
                raise ReminderError("Это время пропускается или повторяется при переводе часов. Укажи время в [UTC].")
            due, cursor = candidates.pop(), absolute.end()
        else:
            raise ReminderError("Когда напомнить? Например: /remind in 15m чай или /remind at 2027-04-15 09:30 [Europe/Moscow] встреча.")
        if due <= now.astimezone(UTC):
            raise ReminderError("Выбери время в будущем.")
        body = text[cursor:].strip().removeprefix("|").strip()
        return Schedule(due_at=due, timezone=zone.key, text=body)
    except ReminderError:
        raise
    except ValueError, KeyError, OverflowError, ValidationError:
        raise ReminderError("Не получилось разобрать дату, часовой пояс или текст. Проверь формат; текст — до 3000 символов.") from None
