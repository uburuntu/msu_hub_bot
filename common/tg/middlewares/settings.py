"""Validated, shared chat preferences with snapshot-safe persistence."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import Chat, TelegramObject
from msu_hub_bot.telemetry import Backend, Boundary, Telemetry

from pydantic import BaseModel, ConfigDict, JsonValue, PrivateAttr, TypeAdapter

from common.db.base import BotRepository
from common.db.observations import chat_observation

_JSON_SETTINGS = TypeAdapter(dict[str, JsonValue])


class Settings(BaseModel):
    model_config = ConfigDict(validate_assignment=True, validate_default=True, extra="allow", hide_input_in_errors=True)

    auto_speech_recognition: bool = True
    auto_video_links: bool = True
    with_nsfw: bool = False

    _chat_id: int | None = PrivateAttr(default=None)
    _saved_snapshot: dict[str, Any] = PrivateAttr(default_factory=dict)
    _save_lock: asyncio.Lock = PrivateAttr(default_factory=asyncio.Lock)

    @property
    def _is_dirty(self) -> bool:
        return self.model_dump() != self._saved_snapshot

    @classmethod
    async def create(cls, db: BotRepository, chat: Chat) -> Settings:
        values = dict(await db.load_settings(chat_observation(chat)))
        for internal in ("_chat_id", "_is_dirty", "_saved_snapshot", "_save_lock"):
            values.pop(internal, None)
        settings = cls.model_validate(values)
        settings._chat_id = chat.id
        settings._saved_snapshot = settings.model_dump()
        return settings

    async def save(self, db: BotRepository, force: bool = False) -> Settings:
        async with self._save_lock:
            if not self._is_dirty and not force:
                return self
            if self._chat_id is None:
                raise RuntimeError("Chat preferences have no persistence identity")
            snapshot = _JSON_SETTINGS.validate_python(self.model_dump())
            changes = {key: value for key, value in snapshot.items() if force or key not in self._saved_snapshot or value != self._saved_snapshot[key]}
            await db.patch_settings(self._chat_id, changes)
            self._saved_snapshot = snapshot
        return self


class SettingsMiddleware(BaseMiddleware):
    def __init__(
        self, db: BotRepository, *, cache_size: int = 128, telemetry: Telemetry | None = None, backend: Backend = Backend.EDGEDB,
    ) -> None:
        if cache_size < 1:
            raise ValueError("Preference cache size must be positive")
        self.db = db
        self.telemetry = telemetry or Telemetry()
        self.backend = backend
        self.cache_size = cache_size
        self.proxies: OrderedDict[int, Settings] = OrderedDict()
        self._active: dict[int, int] = {}
        self._load_lock = asyncio.Lock()

    async def proxy(self, chat: Chat) -> Settings:
        if chat.id in self.proxies:
            self.proxies.move_to_end(chat.id)
            return self.proxies[chat.id]
        with self.telemetry.operation(Boundary.STORAGE, "settings.load", backend=self.backend, trace=False):
            return await self._load(chat)

    async def _load(self, chat: Chat) -> Settings:
        async with self._load_lock:
            if chat.id not in self.proxies:
                self.proxies[chat.id] = await Settings.create(self.db, chat)
            self.proxies.move_to_end(chat.id)
            return self.proxies[chat.id]

    def _trim(self) -> None:
        # Active or unsaved objects must remain shared until their handlers finish.
        for chat_id in list(self.proxies):
            if len(self.proxies) <= self.cache_size:
                break
            if not self._active.get(chat_id) and not self.proxies[chat_id]._is_dirty:
                del self.proxies[chat_id]

    async def __call__(
        self, handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]], event: TelegramObject, data: dict[str, Any]
    ) -> Any:
        data.pop("settings", None)
        chat = getattr(event, "chat", None) or getattr(getattr(event, "message", None), "chat", None)
        if not isinstance(chat, Chat):
            return await handler(event, data)
        preferences = await self.proxy(chat)
        self._active[chat.id] = self._active.get(chat.id, 0) + 1
        data["settings"] = preferences
        try:
            try:
                result = await handler(event, data)
            except BaseException as original:
                try:
                    await self._save(preferences)
                except Exception:
                    original.add_note("Chat preferences also failed to save during cleanup")
                raise
            await self._save(preferences)
            return result
        finally:
            self._active[chat.id] -= 1
            if not self._active[chat.id]:
                del self._active[chat.id]
            self._trim()

    async def close(self) -> None:
        failures: list[Exception] = []
        for preferences in list(self.proxies.values()):
            try:
                await self._save(preferences)
            except Exception as error:
                failures.append(error)
        if failures:
            raise ExceptionGroup("Chat preferences failed to save during shutdown", failures)

    async def _save(self, preferences: Settings) -> None:
        if preferences._is_dirty:
            with self.telemetry.operation(Boundary.STORAGE, "settings.save", backend=self.backend):
                await preferences.save(self.db)
