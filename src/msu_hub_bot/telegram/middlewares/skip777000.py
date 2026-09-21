import asyncio
from collections.abc import Awaitable, Callable
from time import monotonic
from typing import Any

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import ChatMemberUpdated, Message, TelegramObject
from cachetools import TTLCache

from msu_hub_bot.telemetry import Boundary, Outcome, Provider, Telemetry
from msu_hub_bot.telegram.errors import telegram_error_reason


class Skip777000(BaseMiddleware):
    def __init__(self, *, bot_id: int | None = None, telemetry: Telemetry | None = None) -> None:
        self.bot_id = bot_id
        self.telemetry = telemetry or Telemetry()
        self._denied: TTLCache[int, bool] = TTLCache(maxsize=1024, ttl=300, timer=monotonic)
        self._missing: TTLCache[tuple[int, int], bool] = TTLCache(maxsize=1024, ttl=300, timer=monotonic)
        self._retry_until = 0.0
        self._lock = asyncio.Lock()
        self._membership_epoch = 0

    async def __call__(
        self, handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]], event: TelegramObject, data: dict[str, Any]
    ) -> Any:
        if isinstance(event, ChatMemberUpdated):
            bot_id = self.bot_id if self.bot_id is not None else event.bot.id if event.bot is not None else None
            if event.new_chat_member.user.id == bot_id:
                self._membership_epoch += 1
                self._denied.pop(event.chat.id, None)
                for key in list(self._missing):
                    if key[0] == event.chat.id:
                        del self._missing[key]
            return await handler(event, data)
        if isinstance(event, Message) and event.is_automatic_forward:
            async with self._lock:
                key = event.chat.id, event.message_id
                if monotonic() < self._retry_until or event.chat.id in self._denied or key in self._missing:
                    return None
                epoch = self._membership_epoch
                with self.telemetry.operation(Boundary.PROVIDER, "telegram.auto_unpin", provider=Provider.TELEGRAM) as observation:
                    try:
                        await event.unpin()
                    except TelegramRetryAfter as error:
                        self._retry_until = max(self._retry_until, monotonic() + max(1, error.retry_after))
                        observation.set_outcome(Outcome.UNAVAILABLE)
                    except (TelegramBadRequest, TelegramForbiddenError) as error:
                        reason = telegram_error_reason(error)
                        if reason not in {"not_enough_rights", "message_not_found"}:
                            raise
                        observation.set_outcome(Outcome.REJECTED)
                        if epoch == self._membership_epoch:
                            if reason == "not_enough_rights":
                                self._denied[event.chat.id] = True
                            else:
                                self._missing[key] = True
            return None
        return await handler(event, data)
