"""Typed reminder requests and durable delivery state."""

from datetime import UTC, datetime
from typing import Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from msu_hub_bot.storage.features import Payload

DEFAULT_TIMEZONE = "Europe/Moscow"
TEXT_LIMIT = 3000
type ReminderStatus = Literal["pending", "sending", "uncertain", "delivered", "cancelled", "failed"]
type Failure = Literal["rejected", "rate_limit", "uncertain"]


class ReminderError(ValueError):
    """A safe explanation that can be shown to the reminder's owner."""


def valid_zone(value: str) -> str:
    try:
        ZoneInfo(value)
    except ValueError, ZoneInfoNotFoundError:
        raise ValueError("Unknown IANA timezone") from None
    return value


def valid_text(value: str) -> str:
    if len(value.encode("utf-16-le")) // 2 > TEXT_LIMIT:
        raise ValueError("Reminder text exceeds its Telegram budget")
    return value


class Recurrence(Payload):
    kind: Literal["daily", "weekly", "interval"]
    interval_minutes: int | None = Field(default=None, strict=True, ge=15, le=525600)

    @model_validator(mode="after")
    def interval(self) -> Self:
        if (self.kind == "interval") != (self.interval_minutes is not None):
            raise ValueError("Only interval recurrence needs interval_minutes")
        return self


class Schedule(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    due_at: AwareDatetime
    timezone: str = DEFAULT_TIMEZONE
    text: str = ""
    recurrence: Recurrence | None = None

    _zone = field_validator("timezone")(valid_zone)
    _text = field_validator("text")(valid_text)

    @field_validator("due_at")
    @classmethod
    def utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)


class Reminder(Payload):
    author_id: int = Field(gt=0)
    author_name: str = Field(max_length=256)
    chat_id: int
    thread_id: int | None = None
    text: str = Field(min_length=1)
    due_at: AwareDatetime
    timezone: str = DEFAULT_TIMEZONE
    status: ReminderStatus = "pending"
    attempts: int = Field(default=0, ge=0)
    sending_at: AwareDatetime | None = None
    terminal_at: AwareDatetime | None = None
    delivered_at: AwareDatetime | None = None
    delivered_message_id: int | None = None
    failure: Failure | None = None
    recurrence: Recurrence | None = None
    occurrences: int = Field(default=0, ge=0)
    skipped_occurrences: int = Field(default=0, ge=0)

    _zone = field_validator("timezone")(valid_zone)
    _text = field_validator("text")(valid_text)


def upgrade_reminder(value: dict[str, JsonValue]) -> dict[str, JsonValue]:
    return {"recurrence": None, "occurrences": 0, "skipped_occurrences": 0} | value
