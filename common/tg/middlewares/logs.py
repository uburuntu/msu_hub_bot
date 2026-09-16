"""Local aggregate dispatch diagnostics; Telegram content is never logged here."""

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.types import TelegramObject


class LoggingMiddleware(BaseMiddleware):
    def __init__(self) -> None:
        self.logger = logging.getLogger("hub_bot.dispatch")

    async def __call__(
        self, handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]], event: TelegramObject, data: dict[str, Any]
    ) -> Any:
        start = time.monotonic()
        outcome = "error"
        try:
            result = await handler(event, data)
            outcome = "unhandled" if result is UNHANDLED else "handled"
            return result
        finally:
            self.logger.debug(
                "Telegram dispatch completed",
                extra={"outcome": outcome, "duration_ms": round((time.monotonic() - start) * 1000)},
            )
