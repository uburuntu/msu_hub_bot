"""Validated, shared chat preferences with snapshot-safe persistence."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from copy import deepcopy
from typing import Any, Protocol, cast

from aiogram import BaseMiddleware
from aiogram.types import Chat, TelegramObject
from pydantic import BaseModel, ConfigDict, PrivateAttr

from common.db.edb import ChatDB, EdgeDB


class _InsertDatabase(Protocol):
    async def insert_skip_conflict(self, type_name: str, unique_field: str, **values: object) -> object: ...


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
    async def create(cls, db: EdgeDB, chat_id: int) -> Settings:
        row = await ChatDB.query(db).get(chat_id)
        metadata = row.metadata if isinstance(row.metadata, dict) else {}
        values = metadata.get("settings")
        values = dict(values) if isinstance(values, dict) else {}
        for internal in ("_chat_id", "_is_dirty", "_saved_snapshot", "_save_lock"):
            values.pop(internal, None)
        settings = cls.model_validate(values)
        settings._chat_id = chat_id
        settings._saved_snapshot = settings.model_dump()
        return settings

    async def save(self, db: EdgeDB, force: bool = False) -> Settings:
        async with self._save_lock:
            if not self._is_dirty and not force:
                return self
            if self._chat_id is None:
                raise RuntimeError("Chat preferences have no persistence identity")
            snapshot = self.model_dump()
            row = await ChatDB.query(db).get(self._chat_id)
            metadata = deepcopy(row.metadata) if isinstance(row.metadata, dict) else {}
            metadata["settings"] = snapshot
            await ChatDB.query(db).update(self._chat_id, metadata=metadata)
            self._saved_snapshot = snapshot
        return self


class SettingsMiddleware(BaseMiddleware):
    def __init__(self, db: EdgeDB, *, cache_size: int = 128) -> None:
        if cache_size < 1:
            raise ValueError("Preference cache size must be positive")
        self.db = db
        self.cache_size = cache_size
        self.proxies: OrderedDict[int, Settings] = OrderedDict()
        self._active: dict[int, int] = {}
        self._load_lock = asyncio.Lock()

    async def proxy(self, chat: Chat) -> Settings:
        async with self._load_lock:
            if chat.id not in self.proxies:
                await cast(_InsertDatabase, self.db).insert_skip_conflict(
                    "telegram::Chat",
                    "chat_id",
                    chat_id=chat.id,
                    type=chat.type,
                    title=chat.title,
                    username=chat.username,
                    first_name=chat.first_name,
                    last_name=chat.last_name,
                )
                self.proxies[chat.id] = await Settings.create(self.db, chat.id)
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
                    await preferences.save(self.db)
                except Exception:
                    original.add_note("Chat preferences also failed to save during cleanup")
                raise
            await preferences.save(self.db)
            return result
        finally:
            self._active[chat.id] -= 1
            if not self._active[chat.id]:
                del self._active[chat.id]
            self._trim()

    async def close(self) -> None:
        for preferences in list(self.proxies.values()):
            await preferences.save(self.db)
