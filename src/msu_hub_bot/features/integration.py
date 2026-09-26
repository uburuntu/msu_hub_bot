"""Bridge the host's existing state ownership to embedded TeleForge handlers."""

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from msu_hub_bot.telegram.state import UpdateStateContext, release_state_isolation


class HubIsolationBridge(BaseMiddleware):
    """Install after native FSM selection, before a TeleForge handler is invoked."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        selected = data.get("handler")
        if not getattr(selected, "flags", {}).get("feature_key") or "_teleforge_isolation" in data:
            return await handler(event, data)
        state_context = data.get("state_context")
        if not isinstance(state_context, UpdateStateContext):
            raise RuntimeError("Hub state context middleware must precede the TeleForge bridge")

        async def release() -> None:
            release_state_isolation(state_context)

        data["_teleforge_release_isolation"] = release
        return await handler(event, data)
