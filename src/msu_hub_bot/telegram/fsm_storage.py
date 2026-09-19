"""aiogram conversation contracts over versioned, atomic feature documents."""

import asyncio
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import asdict
from datetime import UTC, datetime
import hashlib
import json
from typing import Any
from uuid import uuid4

from aiogram.exceptions import DataNotDictLikeError
from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StateType, StorageKey
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, ValidationError

from msu_hub_bot.storage.features import Conflict, FeatureStore, InvalidPayload, Payload, Record, Scope
from msu_hub_bot.storage.features.store import Transaction
from msu_hub_bot.storage.errors import RepositoryFailure, RepositoryUnavailable

FEATURE = "telegram_state"
MAX_CONFLICT_RETRIES = 12


async def commit_state_change(transaction: Transaction) -> None:
    """Retry uncertain transport outcomes with the same frozen request and receipt."""
    for attempt in range(3):
        try:
            await transaction.commit()
            return
        except RepositoryUnavailable as error:
            if attempt == 2 or error.code not in {RepositoryFailure.UNAVAILABLE, RepositoryFailure.TIMEOUT}:
                raise
            await asyncio.sleep(0.1 * (attempt + 1))


class ConversationKey(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    bot_id: int
    chat_id: int
    user_id: int
    thread_id: int | None = None
    business_connection_id: str | None = None
    destiny: str = "default"

    def storage_key(self) -> StorageKey:
        return StorageKey(**self.model_dump())

    def record_key(self) -> str:
        canonical = json.dumps(self.model_dump(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()

    @property
    def scope(self) -> Scope:
        return Scope(f"bot:{self.bot_id}")


class Conversation(Payload):
    key: ConversationKey
    state: str | None = Field(default=None, strict=True)
    data: dict[str, JsonValue] = Field(default_factory=dict)
    state_expires_at: AwareDatetime | None = None
    data_expires_at: AwareDatetime | None = None

    def current_state(self) -> str | None:
        return self.state if self.state_expires_at is None or self.state_expires_at > datetime.now(UTC) else None

    def current_data(self) -> dict[str, JsonValue]:
        return deepcopy(self.data) if self.data_expires_at is None or self.data_expires_at > datetime.now(UTC) else {}

    def prune_expired(self) -> None:
        now = datetime.now(UTC)
        if self.state_expires_at is not None and self.state_expires_at <= now:
            self.state = None
            self.state_expires_at = None
        if self.data_expires_at is not None and self.data_expires_at <= now:
            self.data = {}
            self.data_expires_at = None

    @property
    def empty(self) -> bool:
        return self.state is None and not self.data and not self.model_extra

    def expiry(self) -> datetime | None:
        deadlines = []
        if self.state is not None:
            deadlines.append(self.state_expires_at)
        if self.data:
            deadlines.append(self.data_expires_at)
        if self.model_extra or not deadlines or None in deadlines:
            return None
        return max(value for value in deadlines if value is not None)


class FeatureFSMStorage(BaseStorage):
    """Borrow FeatureStore; event isolation and connection ownership stay in the app."""

    def __init__(self, store: FeatureStore) -> None:
        self.store = store
        self.records = store.collection(FEATURE, "conversations", Conversation, retention=None)

    async def _get(self, key: ConversationKey) -> Record[Conversation] | None:
        record = await self.records.get(key.scope, key.record_key())
        if record is not None and record.value.key != key:
            raise InvalidPayload()
        return record

    async def get_state(self, key: StorageKey) -> str | None:
        record = await self._get(ConversationKey.model_validate(asdict(key)))
        return None if record is None else record.value.current_state()

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        record = await self._get(ConversationKey.model_validate(asdict(key)))
        return {} if record is None else record.value.current_data()

    async def _mutate(self, key: StorageKey, change: Callable[[Conversation], None]) -> Conversation:
        identity = ConversationKey.model_validate(asdict(key))
        for _ in range(MAX_CONFLICT_RETRIES):
            record = await self._get(identity)
            value = Conversation(key=identity) if record is None else record.value.model_copy(deep=True)
            value.prune_expired()
            try:
                change(value)
            except ValidationError, ValueError, TypeError:
                raise InvalidPayload() from None
            transaction = self.store.transaction(FEATURE, identity.scope, operation_id=str(uuid4()))
            if record is None:
                transaction.expect_absent(self.records.name, identity.record_key())
            else:
                transaction.expect(record)
            if value.empty:
                if record is None:
                    return value
                transaction.delete(record)
            else:
                transaction.put(self.records, identity.record_key(), value, expires_at=value.expiry())
            try:
                await commit_state_change(transaction)
                return value
            except Conflict:
                continue
        raise Conflict()

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        state_value = state.state if isinstance(state, State) else state

        def change(value: Conversation) -> None:
            value.state = state_value
            value.state_expires_at = None

        await self._mutate(key, change)

    @staticmethod
    def _data(data: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise DataNotDictLikeError("Conversation data must be a dict")
        return deepcopy(data)

    async def set_data(self, key: StorageKey, data: Mapping[str, Any]) -> None:
        copied = self._data(data)

        def change(value: Conversation) -> None:
            value.data = copied
            value.data_expires_at = None

        await self._mutate(key, change)

    async def update_data(self, key: StorageKey, data: Mapping[str, Any]) -> dict[str, Any]:
        copied = self._data(data)

        def change(value: Conversation) -> None:
            value.data = {**value.current_data(), **copied}
            value.data_expires_at = None

        result = await self._mutate(key, change)
        return result.current_data()

    async def close(self) -> None:
        pass
