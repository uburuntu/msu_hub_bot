"""Compatibility adapter for the retained EdgeDB schema.

JSON response decoding fixes the old query helper's collection/singleton ambiguity.
Settings use serializable read/modify/write transactions. If historical metadata
is not an object, its exact JSON value is retained in an ``_legacy_metadata``
envelope when the first preference is written; imports never apply this envelope.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import datetime
from types import TracebackType
from typing import Protocol, Self, TypeVar, cast

from pydantic import BaseModel, JsonValue, TypeAdapter, ValidationError

from common.db.edb import EdgeDB
from common.db.models import (
    ArchivedUpdate, ChatObservation, ChatRecord, DirectoryCreate, DirectoryPatch,
    DirectoryRecord, UsageStats, VkPatch, VkSubscription,
)
from msu_hub_bot.settings import Settings, settings


class _Executor(Protocol):
    async def query_single_json(self, query: str, **values: object) -> str: ...
    async def query_json(self, query: str, **values: object) -> str: ...


class _Transaction(_Executor, Protocol):
    async def __aenter__(self) -> Self: ...
    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None,
    ) -> bool | None: ...


class _Client(_Executor, Protocol):
    def transaction(self) -> AsyncIterator[_Transaction]: ...
    async def aclose(self) -> None: ...


_T = TypeVar("_T", bound=BaseModel)
_JSON = TypeAdapter[JsonValue](JsonValue)
_CHAT = "id, created, chat_id, type, title, username, first_name, last_name, metadata"
_DIRECTORY = "id, created, chat_id, name, section, is_hidden, username_alias, members, pinned_message_id"
_VK = "id, created, owner_id, chat_id, last_post_id, with_reposts, with_header, is_suspended, description"
_CHAT_TYPES = {"chat_id": "int64", "type": "str", "title": "str", "username": "str", "first_name": "str", "last_name": "str"}
_USER_TYPES = {
    "user_id": "int64", "is_bot": "bool", "first_name": "str", "last_name": "str", "username": "str", "language_code": "str",
}
_DIRECTORY_TYPES = {
    "chat_id": "int64", "name": "str", "section": "str", "is_hidden": "bool", "username_alias": "str",
    "members": "int32", "pinned_message_id": "int32",
}
_VK_TYPES = {
    "owner_id": "int64", "chat_id": "int64", "last_post_id": "int32", "with_reposts": "bool",
    "with_header": "bool", "is_suspended": "bool", "description": "str",
}


def _decode(raw: str) -> JsonValue:
    try:
        return _JSON.validate_json(raw)
    except ValidationError:
        raise RuntimeError("EdgeDB returned invalid JSON") from None


def _record(model: type[_T], raw: str) -> _T | None:
    value = _decode(raw)
    if value is None:
        return None
    try:
        return model.model_validate(value)
    except ValidationError:
        raise RuntimeError("EdgeDB returned an invalid record") from None


def _required(model: type[_T], raw: str) -> _T:
    record = _record(model, raw)
    if record is None:
        raise RuntimeError("EdgeDB did not return the required record")
    return record


def _records(model: type[_T], raw: str) -> list[_T]:
    value = _decode(raw)
    if not isinstance(value, list):
        raise RuntimeError("EdgeDB did not return a record collection")
    try:
        return [model.model_validate(item) for item in value]
    except ValidationError:
        raise RuntimeError("EdgeDB returned an invalid record collection") from None


def _assignments(values: dict[str, object], types: dict[str, str]) -> str:
    # Identifiers come only from the adapter's fixed field maps, never from input.
    if values.keys() - types.keys():
        raise ValueError("Unsupported persistence field")
    return ", ".join(f"{name} := <{'optional ' if value is None else ''}{types[name]}>${name}" for name, value in values.items())


def _settings(metadata: JsonValue) -> dict[str, JsonValue]:
    value = metadata.get("settings") if isinstance(metadata, dict) else None
    return dict(value) if isinstance(value, dict) else {}


class EdgeDBRepository:
    def __init__(self, *, config: Settings = settings, client: _Client | None = None) -> None:
        self.client = client if client is not None else cast(_Client, EdgeDB(config=config).client)

    async def check(self) -> None:
        if _decode(await self.client.query_single_json("select 1;")) != 1:
            raise RuntimeError("EdgeDB readiness check failed")

    async def close(self) -> None:
        await self.client.aclose()

    async def _ensure_chat(self, executor: _Executor, chat: ChatObservation, *, refresh: bool = True) -> ChatRecord:
        values = {key: value for key, value in chat.model_dump(exclude_unset=True).items() if key in _CHAT_TYPES}
        fields = _assignments(values, _CHAT_TYPES)
        conflict = f"update telegram::Chat set {{{fields}}}" if refresh else "select telegram::Chat"
        raw = await executor.query_single_json(
            f"select (insert telegram::Chat {{{fields}}} unless conflict on .chat_id "
            f"else ({conflict})) {{{_CHAT}}};", **values,
        )
        return _required(ChatRecord, raw)

    async def ensure_chat(self, chat: ChatObservation) -> ChatRecord:
        return await self._ensure_chat(self.client, chat)

    async def _get_chat(self, executor: _Executor, chat_id: int) -> ChatRecord | None:
        return _record(ChatRecord, await executor.query_single_json(
            f"select telegram::Chat {{{_CHAT}}} filter .chat_id = <int64>$chat_id limit 1;", chat_id=chat_id,
        ))

    async def get_chat(self, chat_id: int) -> ChatRecord | None:
        return await self._get_chat(self.client, chat_id)

    async def load_settings(self, chat: ChatObservation) -> dict[str, JsonValue]:
        # A callback's chat may be an old snapshot. Preference loading creates
        # first contact but leaves profile freshness to the archive observations.
        return _settings((await self._ensure_chat(self.client, chat, refresh=False)).metadata)

    async def patch_settings(self, chat_id: int, changes: dict[str, JsonValue]) -> dict[str, JsonValue]:
        result: dict[str, JsonValue] = {}
        async for transaction in self.client.transaction():
            async with transaction:
                row = await self._get_chat(transaction, chat_id)
                if row is None:
                    raise RuntimeError("Chat preferences have no persisted chat")
                result = _settings(row.metadata)
                if not changes:
                    continue
                metadata: dict[str, JsonValue] = dict(row.metadata) if isinstance(row.metadata, dict) else {"_legacy_metadata": row.metadata}
                if "settings" in metadata and not isinstance(metadata["settings"], dict):
                    metadata.setdefault("_legacy_settings", metadata["settings"])
                result.update(changes)
                metadata["settings"] = result
                await transaction.query_single_json(
                    "select (update telegram::Chat filter .chat_id = <int64>$chat_id "
                    "set {metadata := <json>$metadata}) {id};",
                    chat_id=chat_id, metadata=json.dumps(metadata, ensure_ascii=False),
                )
        return result

    async def archive_update(self, update: ArchivedUpdate) -> None:
        # The legacy schema has no normalized messages/membership/topic tables;
        # their wire payload remains in the update archive until cutover.
        async for transaction in self.client.transaction():
            async with transaction:
                for user in update.users:
                    values = {key: value for key, value in user.model_dump(exclude_unset=True).items() if key in _USER_TYPES}
                    fields = _assignments(values, _USER_TYPES)
                    await transaction.query_single_json(
                        f"select (insert telegram::User {{{fields}}} unless conflict on .user_id "
                        f"else (update telegram::User set {{{fields}}})) {{id}};", **values,
                    )
                for chat in update.chats:
                    await self._ensure_chat(transaction, chat)
                await transaction.query_single_json(
                    "select (insert telegram::BotUpdate {data := <json>$data, handled := <bool>$handled, "
                    "created := <datetime>$created}) {id};",
                    data=json.dumps(update.data, ensure_ascii=False), handled=update.handled, created=update.received_at,
                )

    async def statistics(self, since: datetime) -> UsageStats:
        raw = await self.client.query_single_json(
            "select {users := count(telegram::User), chats := count(telegram::Chat), "
            "updates := count((select telegram::BotUpdate filter .created > <datetime>$since)), "
            "handled_updates := count((select telegram::BotUpdate filter .created > <datetime>$since and .handled))};",
            since=since,
        )
        return _required(UsageStats, raw)

    async def list_directory(self) -> list[DirectoryRecord]:
        return _records(DirectoryRecord, await self.client.query_json(f"select msu_hub::EcosystemChat {{{_DIRECTORY}}};"))

    async def get_directory(self, chat_id: int) -> DirectoryRecord | None:
        return _record(DirectoryRecord, await self.client.query_single_json(
            f"select msu_hub::EcosystemChat {{{_DIRECTORY}}} filter .chat_id = <int64>$chat_id limit 1;", chat_id=chat_id,
        ))

    async def create_directory(self, entry: DirectoryCreate) -> DirectoryRecord:
        values = entry.model_dump()
        fields = _assignments(values, _DIRECTORY_TYPES)
        return _required(DirectoryRecord, await self.client.query_single_json(
            f"select (insert msu_hub::EcosystemChat {{{fields}}} unless conflict on .chat_id "
            f"else (select msu_hub::EcosystemChat)) {{{_DIRECTORY}}};", **values,
        ))

    async def patch_directory(self, chat_id: int, changes: DirectoryPatch) -> DirectoryRecord | None:
        values = changes.model_dump(exclude_unset=True)
        if not values:
            return await self.get_directory(chat_id)
        fields = _assignments(values, _DIRECTORY_TYPES)
        return _record(DirectoryRecord, await self.client.query_single_json(
            f"select (update msu_hub::EcosystemChat filter .chat_id = <int64>$chat_id set {{{fields}}}) {{{_DIRECTORY}}};",
            chat_id=chat_id, **values,
        ))

    async def delete_directory(self, chat_id: int) -> bool:
        return bool(_decode(await self.client.query_single_json(
            "select exists (delete msu_hub::EcosystemChat filter .chat_id = <int64>$chat_id);", chat_id=chat_id,
        )))

    async def list_vk_subscriptions(self) -> list[VkSubscription]:
        return _records(VkSubscription, await self.client.query_json(f"select vk_tg::VkWallPosting {{{_VK}}};"))

    async def upsert_vk_subscription(self, owner_id: int, chat_id: int, changes: VkPatch) -> VkSubscription:
        values = {"owner_id": owner_id, "chat_id": chat_id, **changes.model_dump(exclude_unset=True)}
        fields = _assignments(values, _VK_TYPES)
        return _required(VkSubscription, await self.client.query_single_json(
            f"select (insert vk_tg::VkWallPosting {{{fields}}} unless conflict on ((.owner_id, .chat_id)) "
            f"else (update vk_tg::VkWallPosting set {{{fields}}})) {{{_VK}}};", **values,
        ))

    async def advance_vk_cursor(self, owner_id: int, chat_id: int, last_post_id: int) -> None:
        await self.client.query_single_json(
            "select (update vk_tg::VkWallPosting filter .owner_id = <int64>$owner_id and .chat_id = <int64>$chat_id "
            "set {last_post_id := max({.last_post_id, <int32>$last_post_id})}) {id};",
            owner_id=owner_id, chat_id=chat_id, last_post_id=last_post_id,
        )
