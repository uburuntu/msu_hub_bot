"""Freeze explicitly selectable context without collecting message bodies in diagnostics."""

from __future__ import annotations

import json
import re
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from aiogram.types import Message

from msu_hub_bot.feedback.models import FeedbackContext, FeedbackDiagnostic, FeedbackMessage, FeedbackOrigin
from msu_hub_bot.storage.base import BotRepository
from msu_hub_bot.storage.errors import RepositoryError
from msu_hub_bot.storage.models import FeedbackMessageRecord
from msu_hub_bot.telegram.rich_input import rich_text

_AGE = timedelta(days=30)
_MEDIA = (
    "rich_message",
    "animation",
    "photo",
    "video",
    "audio",
    "voice",
    "video_note",
    "sticker",
    "document",
    "contact",
    "venue",
    "location",
    "poll",
    "dice",
)
type DiagnosticOutcome = Literal["completed", "ignored", "cancelled", "failed"]
type _Scope = tuple[int, int, int | None]


def _now() -> datetime:
    return datetime.now(UTC)


def normalized_thread(message: Message) -> int | None:
    """Telegram also supplies raw thread IDs for some ordinary replies."""
    return message.message_thread_id if message.is_topic_message else None


def _supported(message: Message) -> bool:
    return not message.business_connection_id and message.direct_messages_topic is None


def _bounded(value: str, *, characters: int, json_bytes: int) -> str:
    # Both the stored JSON and the plain-text preview have a finite byte budget.
    value = value[:characters].encode("utf-8", errors="replace").decode("utf-8")
    while len(json.dumps(value, ensure_ascii=False).encode("utf-8")) > json_bytes:
        value = value[:-1]
    return value


def _message_snapshot(message: Message) -> FeedbackMessage:
    author_id: int | None = None
    author_kind: Literal["user", "chat", "unknown"] = "unknown"
    author_name = "Unknown"
    if message.sender_chat is not None:
        author_id, author_kind = message.sender_chat.id, "chat"
        author_name = message.sender_chat.title or message.sender_chat.full_name or "Unknown"
    elif message.from_user is not None:
        author_id, author_kind = message.from_user.id, "user"
        author_name = message.from_user.full_name
    text = message.text or message.caption or rich_text(message)
    bounded = _bounded(text, characters=800, json_bytes=850)
    return FeedbackMessage(
        chat_id=message.chat.id,
        message_id=message.message_id,
        sent_at=message.date,
        thread_id=normalized_thread(message),
        author_id=author_id,
        author_kind=author_kind,
        author_name=_bounded(author_name, characters=128, json_bytes=256),
        text=bounded,
        media_kind=next((kind for kind in _MEDIA if getattr(message, kind, None)), None),
        truncated=bounded != text,
    )


def _archived_snapshot(message: FeedbackMessageRecord) -> FeedbackMessage:
    text = _bounded(message.text, characters=800, json_bytes=850)
    return FeedbackMessage(
        **message.model_dump(exclude={"text", "author_name", "truncated"}),
        author_name=_bounded(message.author_name, characters=128, json_bytes=256),
        text=text,
        truncated=message.truncated or text != message.text,
    )


@dataclass(frozen=True)
class _DiagnosticEntry:
    scope: _Scope
    diagnostic: FeedbackDiagnostic
    expires_at: float


class DiagnosticBuffer:
    """A process-local LRU; completed means the selected handler returned normally."""

    def __init__(
        self,
        *,
        max_entries: int = 1000,
        release: str = "",
        clock: Callable[[], datetime] = _now,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if type(max_entries) is not int or not 1 <= max_entries <= 10000:
            raise ValueError("Diagnostics require a bounded capacity")
        self.since = clock()
        self.release = release if len(release) <= 80 and re.fullmatch(r"(?:[a-f0-9]{7,40}|v?\d+\.\d+\.\d+)", release) else None
        self._clock, self._monotonic = clock, monotonic
        self._maximum = max_entries
        self._entries: OrderedDict[tuple[_Scope, int], _DiagnosticEntry] = OrderedDict()

    def _prune(self) -> None:
        now = self._monotonic()
        for key in [key for key, value in self._entries.items() if value.expires_at <= now]:
            del self._entries[key]

    def record(self, message: Message, *, handler: str, command: str, outcome: DiagnosticOutcome) -> None:
        self._prune()
        if (
            not _supported(message)
            or message.from_user is None
            or message.sender_chat is not None
            or message.message_id <= 0
            or re.fullmatch(r"[A-Za-z_][A-Za-z_0-9.]{0,127}", handler) is None
            or re.fullmatch(r"[\w-]{1,64}", command) is None
        ):
            return
        scope = (message.from_user.id, message.chat.id, normalized_thread(message))
        key = (scope, message.message_id)
        self._entries[key] = _DiagnosticEntry(
            scope,
            FeedbackDiagnostic(
                at=self._clock(),
                handler=handler,
                command=command,
                outcome=outcome,
                message_id=message.message_id,
                release=self.release,
            ),
            self._monotonic() + 30 * 60,
        )
        self._entries.move_to_end(key)
        while len(self._entries) > self._maximum:
            self._entries.popitem(last=False)

    def snapshot(self, message: Message, *, before: datetime) -> list[FeedbackDiagnostic]:
        self._prune()
        if not _supported(message) or message.from_user is None or message.sender_chat is not None:
            return []
        scope = (message.from_user.id, message.chat.id, normalized_thread(message))
        selected = sorted(
            (
                (key, value)
                for key, value in self._entries.items()
                if value.scope == scope and value.diagnostic.at <= before and key[1] < message.message_id
            ),
            key=lambda item: (item[1].diagnostic.at, item[0][1]),
        )[-5:]
        for key, _ in selected:
            self._entries.move_to_end(key)
        return [entry.diagnostic.model_copy(deep=True) for _, entry in selected]


async def capture_context(
    message: Message,
    *,
    repository: BotRepository,
    diagnostics: DiagnosticBuffer | None = None,
    now: datetime | None = None,
) -> FeedbackContext:
    """Capture once before preview; unavailable archive context never blocks a draft."""
    frozen = now or _now()
    if frozen.tzinfo is None or frozen.utcoffset() is None:
        raise ValueError("Feedback context requires a timezone-aware timestamp")
    thread_id = normalized_thread(message)
    origin = FeedbackOrigin(
        chat_id=message.chat.id,
        thread_id=thread_id,
        label=_bounded(message.chat.title or message.chat.full_name or "Chat", characters=128, json_bytes=256),
    )
    source = message.reply_to_message
    reply = None
    if (
        source is not None
        and _supported(message)
        and _supported(source)
        and source.chat.id == message.chat.id
        and normalized_thread(source) == thread_id
        and source.message_id > 0
        and frozen - _AGE < source.date <= frozen
    ):
        reply = _message_snapshot(source)
    context = FeedbackContext(
        origin=origin,
        reply=reply,
        reply_available=source is None or reply is not None,
        diagnostics=diagnostics.snapshot(message, before=frozen) if diagnostics is not None else [],
        diagnostics_since=diagnostics.since if diagnostics is not None else None,
        recent_available=_supported(message),
    )
    if not context.recent_available:
        return context
    sent_before = min(message.date, frozen)
    try:
        archived = await repository.recent_feedback_messages(
            message.chat.id, thread_id=thread_id, before=frozen, before_message_id=message.message_id
        )
    except RepositoryError:
        return context.model_copy(update={"recent_available": False})
    # Defend the final snapshot as well as the RPC's bot/chat/topic/age boundary.
    recent = [
        _archived_snapshot(item)
        for item in archived[:5]
        if item.chat_id == message.chat.id
        and item.thread_id == thread_id
        and item.message_id < message.message_id
        and frozen - _AGE < item.sent_at <= sent_before
    ]
    return FeedbackContext(**context.model_dump(exclude={"recent_messages"}), recent_messages=recent)
