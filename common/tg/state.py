"""Topic state eligibility and application-owned, releasable event isolation."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from aiogram import Bot
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.dispatcher.flags import get_flag
from aiogram.dispatcher.middlewares.base import BaseMiddleware
from aiogram.fsm.context import FSMContext
from aiogram.fsm.middleware import FSMContextMiddleware
from aiogram.fsm.storage.base import DEFAULT_DESTINY, BaseEventIsolation, BaseStorage, StorageKey
from aiogram.fsm.strategy import FSMStrategy
from aiogram.types import Message, TelegramObject, Update

Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]


class IsolationScope:
    """A lock lease releasable only by the task which acquired it."""

    def __init__(self, lock: asyncio.Lock, owner: asyncio.Task[Any]) -> None:
        self._lock = lock
        self._owner = owner
        self._released = False

    @property
    def released(self) -> bool:
        return self._released

    def release(self) -> bool:
        if asyncio.current_task() is not self._owner:
            raise RuntimeError("Only the acquiring task may release state isolation")
        if self._released:
            return False
        self._released = True
        self._lock.release()
        return True


@dataclass(slots=True)
class UpdateStateContext:
    eligible: bool
    scope: IsolationScope | None = field(default=None, init=False)
    _owner: asyncio.Task[Any] | None = field(default=None, init=False, repr=False)


_state_context: ContextVar[UpdateStateContext | None] = ContextVar("bot_state_context", default=None)


def release_state_isolation(state_context: UpdateStateContext) -> bool:
    """Release after an audited terminal selection or completed state transition."""
    return state_context.scope.release() if state_context.scope is not None else False


def _context(data: dict[str, Any]) -> UpdateStateContext:
    value = data.get("state_context")
    if not isinstance(value, UpdateStateContext):
        raise RuntimeError("State context middleware must precede FSM middleware")
    return value


def state_eligible(update: Update) -> bool:
    callback = update.callback_query
    return callback is None or isinstance(callback.message, Message)


class StateContextMiddleware(BaseMiddleware):
    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        if not isinstance(event, Update):
            raise TypeError("State context middleware requires an Update")
        context = UpdateStateContext(eligible=state_eligible(event))
        context._owner = asyncio.current_task()
        data["state_context"] = context
        token = _state_context.set(context)
        try:
            return await handler(event, data)
        finally:
            _state_context.reset(token)


@dataclass(slots=True)
class _LockEntry:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


class ReleasableEventIsolation(BaseEventIsolation):
    """Keep one lock per active key, including queued waiters and released scopes."""

    def __init__(self) -> None:
        self._entries: dict[StorageKey, _LockEntry] = {}
        self._owners: dict[asyncio.Task[Any], int] = {}
        self._closing = False
        self._idle = asyncio.Event()
        self._idle.set()

    @property
    def key_count(self) -> int:
        return len(self._entries)

    @asynccontextmanager
    async def lock(self, key: StorageKey) -> AsyncGenerator[None, None]:
        if self._closing:
            raise RuntimeError("State isolation is closed")
        owner = asyncio.current_task()
        if owner is None:
            raise RuntimeError("State isolation requires an asyncio task")
        context = _state_context.get()
        if context is not None and context._owner is not owner:
            context = None  # Inherited job context must not mutate its parent.
        previous = context.scope if context is not None else None
        if previous is not None and not previous.released:
            raise RuntimeError("Release the current state scope before acquiring another")
        entry = self._entries.setdefault(key, _LockEntry())
        entry.users += 1
        self._owners[owner] = self._owners.get(owner, 0) + 1
        self._idle.clear()
        scope: IsolationScope | None = None
        try:
            await entry.lock.acquire()
            scope = IsolationScope(entry.lock, owner)
            if context is not None:
                context.scope = scope
            yield None
        finally:
            if scope is not None:
                scope.release()
                if context is not None:
                    context.scope = previous
            entry.users -= 1
            self._owners[owner] -= 1
            if not self._owners[owner]:
                del self._owners[owner]
            if not entry.users:
                del self._entries[key]
            if not self._entries:
                self._idle.set()

    async def close(self) -> None:
        if asyncio.current_task() in self._owners:
            raise RuntimeError("An active state scope cannot close its isolation")
        self._closing = True
        await self._idle.wait()


class TopicFSMContextMiddleware(FSMContextMiddleware):
    def __init__(self, storage: BaseStorage, events_isolation: BaseEventIsolation) -> None:
        super().__init__(storage, events_isolation, strategy=FSMStrategy.USER_IN_TOPIC)
        self._close_lock = asyncio.Lock()
        self._closed = False

    def resolve_event_context(self, bot: Bot, data: dict[str, Any], destiny: str = DEFAULT_DESTINY) -> FSMContext | None:
        if not _context(data).eligible:
            return None
        return super().resolve_event_context(bot, data, destiny)

    async def close(self) -> None:
        async with self._close_lock:
            if not self._closed:
                await self.events_isolation.close()
                await self.storage.close()
                self._closed = True


class SelectiveIsolationMiddleware(BaseMiddleware):
    """The release flag promises no later FSM access or SkipHandler continuation."""

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        release = get_flag(data, "fsm_release") is True
        if release:
            release_state_isolation(_context(data))
        try:
            return await handler(event, data)
        except SkipHandler:
            if release:
                raise RuntimeError("A released terminal handler cannot skip to another route") from None
            raise
