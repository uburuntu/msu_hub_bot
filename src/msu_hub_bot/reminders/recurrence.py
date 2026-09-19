"""Bounded recurrence arithmetic, preserving local calendar time across DST."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from .models import Recurrence, ReminderError


def next_occurrence(due_at: datetime, timezone: str, recurrence: Recurrence, now: datetime) -> tuple[datetime, int]:
    """Coalesce missed occurrences; return the next future time and skipped count.

    Calendar repeats skip nonexistent wall times and use the first occurrence
    of an ambiguous wall time. Interval repeats measure elapsed UTC minutes.
    """
    if recurrence.kind == "interval":
        assert recurrence.interval_minutes is not None
        step = timedelta(minutes=recurrence.interval_minutes)
        count = max(1, (now - due_at) // step + 1)
        try:
            return due_at + count * step, count - 1
        except OverflowError:
            raise ReminderError("Следующее повторение выходит за диапазон дат.") from None
    zone = ZoneInfo(timezone)
    origin = due_at.astimezone(zone).replace(tzinfo=None)
    local_now = now.astimezone(zone).replace(tzinfo=None)
    days = 1 if recurrence.kind == "daily" else 7
    count = max(1, (local_now.date() - origin.date()).days // days)
    try:
        for _ in range(4):
            naive = origin + timedelta(days=count * days)
            candidate = naive.replace(tzinfo=zone, fold=0).astimezone(UTC)
            if candidate.astimezone(zone).replace(tzinfo=None) == naive and candidate > now:
                return candidate, count - 1
            count += 1
    except OverflowError:
        pass
    raise ReminderError("Не удалось выбрать следующее повторение. Выбери другое время.")
