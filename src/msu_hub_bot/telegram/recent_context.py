"""Bounded, process-local human text context; never stores Telegram objects."""

import math
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject

from msu_hub_bot.providers.jev import MAX_CONTEXT_CHARS, MAX_CONTEXT_MESSAGES


@dataclass(frozen=True, slots=True, repr=False)
class _Entry:
    message_id: int
    sent_at: float
    expires_at: float
    text: str


class RecentMessages:
    """Keep short snippets in bounded chat/topic buckets, expiring on access."""

    def __init__(
        self,
        *,
        capacity: int = 256,
        per_topic: int = 12,
        max_chars: int = MAX_CONTEXT_CHARS,
        ttl_seconds: float = 24 * 60 * 60,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if (
            type(capacity) is not int
            or not 1 <= capacity <= 1024
            or type(per_topic) is not int
            or not 1 <= per_topic <= 32
            or type(max_chars) is not int
            or not 1 <= max_chars <= MAX_CONTEXT_CHARS
            or isinstance(ttl_seconds, bool)
            or not math.isfinite(ttl_seconds)
            or not 0 < ttl_seconds <= 24 * 60 * 60
        ):
            raise ValueError("Invalid recent-message limits")
        self._capacity, self._per_topic, self._max_chars = capacity, per_topic, max_chars
        self._ttl, self._clock = ttl_seconds, clock
        self._topics: OrderedDict[tuple[int, int | None], list[_Entry]] = OrderedDict()

    @staticmethod
    def _key(message: Message) -> tuple[int, int | None]:
        return message.chat.id, message.message_thread_id if message.is_topic_message else None

    def _expire(self) -> float:
        now = self._clock()
        for key, entries in tuple(self._topics.items()):
            current = [entry for entry in entries if entry.expires_at > now]
            if current:
                self._topics[key] = current
            else:
                del self._topics[key]
        return now

    def remember(self, message: Message) -> None:
        now = self._expire()
        if message.from_user is None or message.from_user.is_bot or message.sender_chat is not None:
            return
        text = message.text or message.caption
        if not text or not text.strip():
            return
        key = self._key(message)
        entries = [entry for entry in self._topics.pop(key, []) if entry.message_id != message.message_id]
        entries.append(_Entry(message.message_id, message.date.timestamp(), now + self._ttl, text[: self._max_chars]))
        entries.sort(key=lambda entry: (entry.sent_at, entry.message_id))
        self._topics[key] = entries[-self._per_topic :]
        while len(self._topics) > self._capacity:
            self._topics.popitem(last=False)

    def before(self, message: Message, limit: int = MAX_CONTEXT_MESSAGES) -> tuple[str, ...]:
        if type(limit) is not int or not 0 <= limit <= MAX_CONTEXT_MESSAGES:
            raise ValueError("Recent context is limited to five messages")
        self._expire()
        if not limit:
            return ()
        reply = message.reply_to_message
        reply_id = reply.message_id if reply is not None and reply.chat.id == message.chat.id else None
        entries = self._topics.get(self._key(message), [])
        return tuple(
            entry.text
            for entry in entries
            if entry.message_id < message.message_id and entry.message_id != reply_id and entry.sent_at <= message.date.timestamp()
        )[-limit:]


class RecentMessagesMiddleware(BaseMiddleware):
    """Observe new messages before handler selection, without network or tasks."""

    def __init__(self, recent_messages: RecentMessages) -> None:
        self.recent_messages = recent_messages

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, Message):
            self.recent_messages.remember(event)
        return await handler(event, data)
