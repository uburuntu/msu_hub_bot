"""Attach explicit Telegram identifiers and selected command names at shared boundaries."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.dispatcher.flags import get_flag
from aiogram.filters.command import CommandObject
from aiogram.types import (
    CallbackQuery,
    Chat,
    InaccessibleMessage,
    Message,
    MessageReactionCountUpdated,
    MessageReactionUpdated,
    TelegramObject,
    Update,
    User,
)

from msu_hub_bot.telemetry import Boundary, Outcome, Telemetry
from msu_hub_bot.feedback.context import DiagnosticBuffer, DiagnosticOutcome
from msu_hub_bot.telegram.filters import MetaInfo

Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]


def _message(event: TelegramObject) -> Message | InaccessibleMessage | None:
    if isinstance(event, Message):
        return event
    if isinstance(event, CallbackQuery):
        return event.message
    return None


class DispatchTelemetryMiddleware(BaseMiddleware):
    def __init__(self, telemetry: Telemetry) -> None:
        self.telemetry = telemetry

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        kind = "unknown"
        update_id = None
        target = event
        if isinstance(event, Update):
            update_id = event.update_id
            try:
                kind = event.event_type
                target = event.event
            except Exception:
                pass
        message = _message(target)
        reaction = target if isinstance(target, (MessageReactionUpdated, MessageReactionCountUpdated)) else None
        actor_chat = target.actor_chat if isinstance(target, MessageReactionUpdated) else None
        user = data.get("event_from_user", getattr(target, "from_user", None))
        chat = data.get("event_chat", message.chat if message is not None else getattr(target, "chat", None))
        with (
            self.telemetry.context(
                user_id=user.id if isinstance(user, User) else None,
                actor_chat_id=actor_chat.id if actor_chat is not None else None,
                chat_id=chat.id if isinstance(chat, Chat) else None,
                message_id=message.message_id if message is not None else reaction.message_id if reaction is not None else None,
                thread_id=message.message_thread_id if isinstance(message, Message) else None,
                update_id=update_id,
                reply_to_message_id=message.reply_to_message.message_id
                if isinstance(message, Message) and message.reply_to_message
                else None,
            ),
            self.telemetry.dispatch(kind) as observation,
        ):
            result = await handler(event, data)
            if result is UNHANDLED:
                observation.outcome = Outcome.IGNORED
            return result


class HandlerTelemetryMiddleware(BaseMiddleware):
    def __init__(self, telemetry: Telemetry, *, diagnostics: DiagnosticBuffer | None = None) -> None:
        self.telemetry = telemetry
        self.diagnostics = diagnostics

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        key = get_flag(data, "handler_key")
        meta = data.get("meta")
        parsed = data.get("command")
        command = meta.keyword if isinstance(meta, MetaInfo) else parsed.command if isinstance(parsed, CommandObject) else None
        command_kind = "hashtag" if isinstance(meta, MetaInfo) and meta.hashtag else "slash"
        outcome: DiagnosticOutcome = "failed"
        with (
            self.telemetry.context(command=command, command_kind=command_kind),
            self.telemetry.operation(Boundary.HANDLER, key if isinstance(key, str) else "unknown") as observation,
        ):
            try:
                result = await handler(event, data)
                outcome = "ignored" if result is UNHANDLED else "completed"
                if result is UNHANDLED:
                    observation.set_outcome(Outcome.IGNORED)
                return result
            except asyncio.CancelledError:
                outcome = "cancelled"
                raise
            finally:
                if self.diagnostics is not None and isinstance(event, Message) and isinstance(key, str) and command:
                    self.diagnostics.record(event, handler=key, command=command, outcome=outcome)
