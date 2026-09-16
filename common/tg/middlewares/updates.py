"""Owned, bounded persistence of Telegram update history and directory metadata."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, Protocol

from aiogram import BaseMiddleware
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.dispatcher.middlewares.user_context import UserContextMiddleware
from aiogram.types import Chat, TelegramObject, Update

from common.tg.runtime import Supervisor


class ArchiveDatabase(Protocol):
    async def upsert(self, type_name: str, unique_field: str, **values: Any) -> object: ...
    async def insert(self, type_name: str, **values: Any) -> object: ...


def _telegram_json(value: Any) -> Any:
    if isinstance(value, datetime):
        return int(value.timestamp())
    if isinstance(value, dict):
        return {key: _telegram_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_telegram_json(item) for item in value]
    return value


class UpdatesMiddleware(BaseMiddleware):
    def __init__(self, db: ArchiveDatabase, supervisor: Supervisor, *, concurrency: int = 100, pending_limit: int = 10_000) -> None:
        if concurrency < 1 or pending_limit < 0:
            raise ValueError("Archive concurrency must be positive and pending limit nonnegative")
        self.db = db
        self.supervisor = supervisor
        self._workers = asyncio.Semaphore(concurrency)
        self._slots = asyncio.Semaphore(concurrency + pending_limit)

    async def _chat(self, chat: Chat) -> None:
        await self.db.upsert(
            "telegram::Chat",
            "chat_id",
            chat_id=chat.id,
            type=chat.type,
            title=chat.title,
            username=chat.username,
            first_name=chat.first_name,
            last_name=chat.last_name,
        )

    async def _archive(self, update: Update, handled: bool) -> None:
        async with self._workers:
            failures: list[Exception] = []

            async def attempt(operation: Awaitable[object]) -> None:
                try:
                    await operation
                except Exception as error:
                    failures.append(error)

            context = UserContextMiddleware.resolve_event_context(update)
            if user := context.user:
                await attempt(
                    self.db.upsert(
                        "telegram::User",
                        "user_id",
                        user_id=user.id,
                        is_bot=user.is_bot,
                        first_name=user.first_name,
                        last_name=user.last_name,
                        username=user.username,
                        language_code=user.language_code,
                    )
                )
            message = update.message or update.edited_message or update.channel_post or update.edited_channel_post
            if message is not None and message.sender_chat is not None:
                await attempt(self._chat(message.sender_chat))
            if context.chat is not None:
                await attempt(self._chat(context.chat))
            payload = _telegram_json(update.model_dump(mode="python", by_alias=True, exclude_none=True))
            await attempt(self.db.insert("telegram::BotUpdate", data=payload, handled=handled))
            if failures:
                raise ExceptionGroup("Update archival operations failed", failures)

    async def _submit(self, update: Update, handled: bool) -> None:
        await self._slots.acquire()
        try:
            task = self.supervisor.create_job(lambda: self._archive(update, handled))
        except BaseException:
            self._slots.release()
            raise
        task.add_done_callback(lambda task: self._slots.release())

    async def __call__(
        self, handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]], event: TelegramObject, data: dict[str, Any]
    ) -> Any:
        if not isinstance(event, Update):
            raise TypeError("Update archive middleware requires an Update")
        try:
            result = await handler(event, data)
        except BaseException as original:
            try:
                await self._submit(event, False)
            except Exception:
                original.add_note("Update history could not be queued during cleanup")
            raise
        await self._submit(event, result is not UNHANDLED)
        return result
