"""Observe dispatch and selected handlers without inspecting Telegram content."""

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.dispatcher.flags import get_flag
from aiogram.types import TelegramObject, Update

from msu_hub_bot.telemetry import Boundary, Outcome, Telemetry

Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]


class DispatchTelemetryMiddleware(BaseMiddleware):
    def __init__(self, telemetry: Telemetry) -> None:
        self.telemetry = telemetry

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        kind = "unknown"
        if isinstance(event, Update):
            try:
                kind = event.event_type
            except Exception:
                pass
        with self.telemetry.dispatch(kind) as observation:
            result = await handler(event, data)
            if result is UNHANDLED:
                observation.outcome = Outcome.IGNORED
            return result


class HandlerTelemetryMiddleware(BaseMiddleware):
    def __init__(self, telemetry: Telemetry) -> None:
        self.telemetry = telemetry

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        key = get_flag(data, "handler_key")
        with self.telemetry.operation(Boundary.HANDLER, key if isinstance(key, str) else "unknown") as observation:
            result = await handler(event, data)
            if result is UNHANDLED:
                observation.set_outcome(Outcome.IGNORED)
            return result
