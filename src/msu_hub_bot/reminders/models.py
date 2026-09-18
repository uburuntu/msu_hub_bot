"""Typed reminder requests and durable delivery state."""

from datetime import UTC, datetime
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

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


class Schedule(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    due_at: AwareDatetime
    timezone: str = DEFAULT_TIMEZONE
    text: str = ""

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

    _zone = field_validator("timezone")(valid_zone)
    _text = field_validator("text")(valid_text)
