"""Owned, bounded persistence of Telegram update history and directory metadata."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from aiogram import BaseMiddleware
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.types import TelegramObject, Update

from common.db.base import BotRepository
from common.db.observations import archive_observation
from common.tg.runtime import Supervisor
from msu_hub_bot.telemetry import Backend, Boundary, Telemetry


class UpdatesMiddleware(BaseMiddleware):
    def __init__(
        self,
        db: BotRepository,
        supervisor: Supervisor,
        *,
        concurrency: int = 100,
        pending_limit: int = 10_000,
        telemetry: Telemetry | None = None,
        backend: Backend = Backend.EDGEDB,
    ) -> None:
        if concurrency < 1 or pending_limit < 0:
            raise ValueError("Archive concurrency must be positive and pending limit nonnegative")
        self.db = db
        self.telemetry = telemetry or Telemetry()
        self.backend = backend
        self.supervisor = supervisor
        self._workers = asyncio.Semaphore(concurrency)
        self._slots = asyncio.Semaphore(concurrency + pending_limit)

    async def _archive(self, update: Update, handled: bool, received_at: datetime) -> None:
        async with self._workers:
            with self.telemetry.operation(Boundary.STORAGE, "archive.write", backend=self.backend, trace=False):
                await self.db.archive_update(archive_observation(update, handled, received_at=received_at))

    async def _submit(self, update: Update, handled: bool, received_at: datetime) -> None:
        await self._slots.acquire()
        try:
            task = self.supervisor.create_job(lambda: self._archive(update, handled, received_at), trace=False)
        except BaseException:
            self._slots.release()
            raise
        task.add_done_callback(lambda task: self._slots.release())

    async def __call__(
        self, handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]], event: TelegramObject, data: dict[str, Any]
    ) -> Any:
        if not isinstance(event, Update):
            raise TypeError("Update archive middleware requires an Update")
        received_at = datetime.now(UTC)
        try:
            result = await handler(event, data)
        except BaseException as original:
            try:
                await self._submit(event, False, received_at)
            except Exception:
                original.add_note("Update history could not be queued during cleanup")
            raise
        await self._submit(event, result is not UNHANDLED, received_at)
        return result
