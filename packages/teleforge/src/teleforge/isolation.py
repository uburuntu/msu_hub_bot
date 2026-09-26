"""Terminal release of an already selected handler's native FSM lock."""

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AsyncExitStack
from typing import Any, Self

from aiogram.fsm.storage.base import BaseStorage, StateType, StorageKey


class IsolationError(RuntimeError):
    """A terminal handler tried to reuse state or an incompatible host lock."""


class IsolationScope:
    def __init__(self, stack: AsyncExitStack) -> None:
        self._stack = stack
        self._owner = asyncio.current_task()
        self.released = False

    @classmethod
    def from_host(cls, release: Callable[[], Awaitable[object]]) -> Self:
        """Bridge a host-owned lock already held through state lookup and selection."""
        stack = AsyncExitStack()
        stack.push_async_callback(release)
        return cls(stack)

    def check(self) -> None:
        if self.released:
            raise IsolationError("FSM access is forbidden after terminal isolation release")
        if asyncio.current_task() is not self._owner:
            raise IsolationError("FSM scope belongs to the selected update task")

    async def release(self) -> None:
        if asyncio.current_task() is not self._owner:
            raise IsolationError("Only the selected update task may release its FSM isolation")
        if not self.released:
            self.released = True
            await self._stack.aclose()


class ScopedStorage(BaseStorage):
    """Guard both injected FSMContext and its storage after terminal release."""

    def __init__(self, storage: BaseStorage, scope: IsolationScope) -> None:
        self._storage, self._scope = storage, scope

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        self._scope.check()
        await self._storage.set_state(key, state)

    async def get_state(self, key: StorageKey) -> str | None:
        self._scope.check()
        return await self._storage.get_state(key)

    async def set_data(self, key: StorageKey, data: Mapping[str, Any]) -> None:
        self._scope.check()
        await self._storage.set_data(key, data)

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        self._scope.check()
        return await self._storage.get_data(key)

    async def update_data(self, key: StorageKey, data: Mapping[str, Any]) -> dict[str, Any]:
        self._scope.check()
        return await self._storage.update_data(key, data)

    async def close(self) -> None:
        raise IsolationError("The application owns FSM storage lifetime")
