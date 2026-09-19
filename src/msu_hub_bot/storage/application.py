"""Permanent application documents behind the bot's settings and directory API."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from uuid import uuid4

from pydantic import Field, JsonValue, TypeAdapter, ValidationError

from msu_hub_bot.storage.errors import RepositoryError, RepositoryFailure, RepositoryUnavailable
from msu_hub_bot.storage.features import Collection, Conflict, FeatureProtocolError, FeatureStore, InvalidPayload, Payload, Scope
from msu_hub_bot.storage.features.models import CommitResult, Record
from msu_hub_bot.storage.features.store import Transaction
from msu_hub_bot.storage.models import (
    BigInt,
    ChatRecord,
    DirectoryCreate,
    DirectoryPatch,
    DirectoryRecord,
    VkPatch,
    VkSubscription,
    utc_now,
)

APPLICATION = Scope("global", owner="application")
_BIGINT = TypeAdapter(BigInt)
_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])
_ATTEMPTS = 8


class ChatPreferences(Payload):
    auto_speech_recognition: bool = True
    auto_video_links: bool = True
    with_nsfw: bool = False


class DirectoryDocument(DirectoryRecord, Payload):
    model_config = Payload.model_config


class VkDocument(VkSubscription, Payload):
    model_config = Payload.model_config

    thread_id: int | None = Field(default=None, gt=0)
    title: str = Field(default="", max_length=160)
    include_keywords: list[str] = Field(default_factory=list, max_length=20)
    exclude_keywords: list[str] = Field(default_factory=list, max_length=20)
    archived: bool = False
    created_by: int | None = Field(default=None, gt=0)
    updated_by: int | None = Field(default=None, gt=0)
    creation_request_id: str | None = None
    creation_fingerprint: str | None = None


def vk_key(owner_id: int, chat_id: int, thread_id: int | None = None) -> str:
    original = f"{_key(owner_id)}:{_key(chat_id)}"
    return original if thread_id is None else f"{original}:topic:{thread_id}"


def upgrade_vk(value: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Add topic configuration without changing original flags, cursor or identity."""
    return {"thread_id": None, "title": "", "include_keywords": [], "exclude_keywords": [], "archived": False} | value


def _key(value: int) -> str:
    try:
        return str(_BIGINT.validate_python(value, strict=True))
    except ValidationError:
        raise ValueError("A stored identifier must be a signed 64-bit integer") from None


def _validate[M: Payload](model: type[M], value: object) -> M:
    try:
        return model.model_validate(value)
    except ValidationError:
        raise InvalidPayload() from None


def _identity[M: Payload](record: Record[M]) -> None:
    value = record.value
    if isinstance(value, DirectoryDocument) and record.key != str(value.chat_id):
        raise FeatureProtocolError()
    if isinstance(value, VkDocument) and record.key != vk_key(value.owner_id, value.chat_id, value.thread_id):
        raise FeatureProtocolError()


class ApplicationDocuments:
    """CAS updates preserve unrelated fields and retain the original entity identity."""

    def __init__(self, store: FeatureStore, chat_lookup: Callable[[int], Awaitable[ChatRecord | None]]) -> None:
        self.store = store
        self.chat_lookup = chat_lookup
        self.settings = store.collection("settings", "chats", ChatPreferences, retention=None)
        self.directory = store.collection("ecosystem", "chats", DirectoryDocument, retention=None)
        self.subscriptions = store.collection("vk", "subscriptions", VkDocument, retention=None, version=2, upgrades={1: upgrade_vk})

    @staticmethod
    async def _commit(tx: Transaction) -> CommitResult:
        try:
            return await tx.commit()
        except RepositoryUnavailable as error:
            if error.code is RepositoryFailure.CLOSED:
                raise
        except TimeoutError:
            pass
        # The receipt resolves a lost response; never rebuild an uncertain write.
        return await tx.commit()

    async def _change[M: Payload](self, collection: Collection[M], key: str, transform: Callable[[M | None], M | None]) -> M | None:
        for _ in range(_ATTEMPTS):
            previous = await collection.get(APPLICATION, key)
            if previous is not None:
                _identity(previous)
            value = transform(None if previous is None else previous.value)
            if value is None:
                return None
            if previous is not None and value == previous.value:
                return previous.value
            tx = self.store.transaction(collection.feature, APPLICATION, operation_id=uuid4().hex)
            if previous is None:
                tx.expect_absent(collection.name, key)
            else:
                tx.expect(previous)
            if isinstance(value, VkDocument):
                tx.put(collection, key, value, parent=f"chat:{value.chat_id}:topic:{value.thread_id or 0}")
            else:
                tx.put(collection, key, value)
            try:
                result = await self._commit(tx)
            except Conflict:
                continue
            record = collection.decode(result.records[0], APPLICATION)
            _identity(record)
            return record.value
        raise Conflict()

    @staticmethod
    async def _all[M: Payload](collection: Collection[M]) -> list[M]:
        result: list[M] = []
        after = None
        while True:
            page = await collection.list(APPLICATION, after=after, limit=200)
            for record in page:
                _identity(record)
                result.append(record.value)
            if len(page) < 200:
                return result
            after = page[-1].key

    @staticmethod
    def _seed(chat: ChatRecord) -> dict[str, JsonValue]:
        values = chat.metadata.get("settings") if isinstance(chat.metadata, dict) else None
        return values if isinstance(values, dict) else {}

    async def load_settings(self, chat: ChatRecord) -> dict[str, JsonValue]:
        value = await self._change(
            self.settings, _key(chat.chat_id), lambda current: current or _validate(ChatPreferences, self._seed(chat))
        )
        assert value is not None
        return _JSON_OBJECT.validate_python(value.model_dump(mode="json"), strict=True)

    async def patch_settings(self, chat_id: int, changes: dict[str, JsonValue]) -> dict[str, JsonValue]:
        key = _key(chat_id)
        chat = await self.chat_lookup(chat_id)
        if chat is None:
            raise RepositoryError(RepositoryFailure.REJECTED)

        def patch(current: ChatPreferences | None) -> ChatPreferences:
            original = self._seed(chat) if current is None else current.model_dump(mode="json")
            return _validate(ChatPreferences, original | changes)

        value = await self._change(self.settings, key, patch)
        assert value is not None
        return _JSON_OBJECT.validate_python(value.model_dump(mode="json"), strict=True)

    async def list_directory(self) -> list[DirectoryRecord]:
        return sorted(await self._all(self.directory), key=lambda entry: entry.chat_id)

    async def get_directory(self, chat_id: int) -> DirectoryRecord | None:
        record = await self.directory.get(APPLICATION, _key(chat_id))
        if record is None:
            return None
        _identity(record)
        return record.value

    async def create_directory(self, entry: DirectoryCreate) -> DirectoryRecord:
        created = _validate(DirectoryDocument, entry.model_dump() | {"id": uuid4(), "created": utc_now()})
        value = await self._change(self.directory, _key(entry.chat_id), lambda current: current or created)
        assert value is not None
        return value

    async def patch_directory(self, chat_id: int, changes: DirectoryPatch) -> DirectoryRecord | None:
        patch = changes.model_dump(mode="json", exclude_unset=True)
        return await self._change(
            self.directory,
            _key(chat_id),
            lambda current: None if current is None else _validate(DirectoryDocument, current.model_dump(mode="json") | patch),
        )

    async def delete_directory(self, chat_id: int) -> bool:
        key = _key(chat_id)
        for _ in range(_ATTEMPTS):
            current = await self.directory.get(APPLICATION, key)
            if current is None:
                return False
            _identity(current)
            tx = self.store.transaction(self.directory.feature, APPLICATION, operation_id=uuid4().hex)
            tx.delete(current)
            try:
                await self._commit(tx)
            except Conflict:
                continue
            return True
        raise Conflict()

    async def list_vk_subscriptions(self) -> list[VkSubscription]:
        return sorted(await self._all(self.subscriptions), key=lambda item: (item.owner_id, item.chat_id))

    async def upsert_vk_subscription(self, owner_id: int, chat_id: int, changes: VkPatch) -> VkSubscription:
        key = f"{_key(owner_id)}:{_key(chat_id)}"
        patch = changes.model_dump(mode="json", exclude_unset=True)
        created = VkDocument(
            id=uuid4(),
            created=utc_now(),
            owner_id=owner_id,
            chat_id=chat_id,
            last_post_id=0,
            with_reposts=False,
            with_header=True,
            is_suspended=True,
        )
        value = await self._change(
            self.subscriptions, key, lambda current: _validate(VkDocument, (current or created).model_dump(mode="json") | patch)
        )
        assert value is not None
        return value

    async def advance_vk_cursor(self, owner_id: int, chat_id: int, last_post_id: int) -> None:
        key = f"{_key(owner_id)}:{_key(chat_id)}"
        _key(last_post_id)
        await self._change(
            self.subscriptions,
            key,
            lambda current: (
                None if current is None else current.model_copy(update={"last_post_id": max(current.last_post_id, last_post_id)})
            ),
        )
