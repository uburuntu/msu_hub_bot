"""Paused VK targets over existing feature documents; preview never publishes."""

import asyncio
import hashlib
import json
import re
from datetime import UTC, datetime
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from msu_hub_bot.providers.vk.api import VkApi, public_source
from msu_hub_bot.providers.vk.models import Resolution, Wall
from msu_hub_bot.storage.application import APPLICATION, ApplicationDocuments, VkDocument, upgrade_vk, vk_key
from msu_hub_bot.storage.features import Conflict, FeatureProtocolError, FeatureStore, Payload, Record

AUTOMATIC_POSTING = False


class RepostError(ValueError):
    """Safe, user-visible configuration errors."""


class RepostOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    with_reposts: bool = False
    with_header: bool = True
    include_keywords: list[str] = Field(default_factory=list, max_length=20)
    exclude_keywords: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("include_keywords", "exclude_keywords")
    @classmethod
    def keywords(cls, values: list[str]) -> list[str]:
        result = list(dict.fromkeys(value.strip() for value in values if value.strip()))
        if any(len(value) > 80 for value in result):
            raise ValueError("Keyword is too long")
        return result

    def selects(self, text: str, is_repost: bool) -> bool:
        text = text.casefold()
        return (
            (self.with_reposts or not is_repost)
            and (not self.include_keywords or any(word.casefold() in text for word in self.include_keywords))
            and not any(word.casefold() in text for word in self.exclude_keywords)
        )


class SourcePreview(RepostOptions):
    source: str = Field(min_length=1, max_length=256)


class RepostCreate(SourcePreview):
    request_id: UUID
    title: str = Field(default="", max_length=160)


class RepostUpdate(RepostOptions):
    etag: UUID
    title: str = Field(default="", max_length=160)
    archived: bool = False


class RepostRequest(Payload):
    target_key: str
    fingerprint: str


def source_url(owner_id: int) -> str:
    return f"https://vk.com/{'club' if owner_id < 0 else 'id'}{abs(owner_id)}"


class Reposts:
    def __init__(self, store: FeatureStore, api: VkApi | None = None) -> None:
        self.store, self.api = store, api
        self.items = store.collection("vk", "subscriptions", VkDocument, retention=None, version=2, upgrades={1: upgrade_vk})
        self.requests = store.collection("vk", "requests", RepostRequest, retention=None)
        self._providers = asyncio.Semaphore(2)

    async def resolve(self, source: str) -> int:
        source = source.strip()
        if re.fullmatch(r"-?[1-9][0-9]{0,14}", source):
            return int(source)
        url = urlsplit(source if "://" in source else "https://" + source)
        if (
            url.scheme != "https"
            or url.hostname not in {"vk.com", "www.vk.com", "m.vk.com", "vk.ru", "www.vk.ru"}
            or url.username
            or url.password
            or url.port is not None
            or url.query
            or url.fragment
        ):
            raise RepostError("Укажи ID стены или ссылку на страницу VK без дополнительных параметров.")
        name = url.path.strip("/")
        if found := re.fullmatch(r"(club|public|id)([1-9][0-9]{0,14})", name):
            return int(found[2]) * (1 if found[1] == "id" else -1)
        if found := re.fullmatch(r"wall(-?[1-9][0-9]{0,14})(?:_[1-9][0-9]*)?", name):
            return int(found[1])
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.]{0,63}", name):
            raise RepostError("Не удалось распознать адрес стены VK.")
        if self.api is None:
            raise RepostError("Для короткой ссылки нужен доступ к VK. Можно сохранить числовой ID стены.")
        try:
            async with asyncio.timeout(6), self._providers:
                raw: object = await self.api.request("utils.resolveScreenName", screen_name=name)
            resolved = Resolution.model_validate(raw)
        except Exception:
            raise RepostError("VK не подтвердил страницу. Попробуй её числовой ID или повтори позже.") from None
        return resolved.object_id * (1 if resolved.type == "user" else -1)

    @staticmethod
    def _identity(record: Record[VkDocument]) -> None:
        value = record.value
        if record.key != vk_key(value.owner_id, value.chat_id, value.thread_id):
            raise FeatureProtocolError()

    async def list(self, chat_id: int, thread_id: int | None) -> list[Record[VkDocument]]:
        result: list[Record[VkDocument]] = []
        after = None
        for _ in range(25):
            rows = await self.items.list(APPLICATION, parent=f"chat:{chat_id}:topic:{thread_id or 0}", after=after, limit=200)
            for row in rows:
                self._identity(row)
                if (row.value.chat_id, row.value.thread_id) != (chat_id, thread_id):
                    raise FeatureProtocolError()
                result.append(row)
            if len(rows) < 200:
                return sorted(result, key=lambda row: (row.value.archived, row.value.title.casefold(), row.key))
            after = rows[-1].key
        raise RepostError("В этой теме слишком много подписок для одного списка.")

    async def create(self, user_id: int, chat_id: int, thread_id: int | None, body: RepostCreate) -> Record[VkDocument]:
        request_key = f"{user_id}:{body.request_id}"
        fingerprint = hashlib.sha256(
            json.dumps(body.model_dump(mode="json") | {"chat_id": chat_id, "thread_id": thread_id}, sort_keys=True).encode()
        ).hexdigest()
        receipt = await self.requests.get(APPLICATION, request_key)
        if receipt is not None:
            if receipt.value.fingerprint != fingerprint:
                raise Conflict()
            saved = await self.items.get(APPLICATION, receipt.value.target_key)
            if saved is None:
                raise FeatureProtocolError()
            self._identity(saved)
            if (saved.value.chat_id, saved.value.thread_id, saved.value.created_by) != (chat_id, thread_id, user_id):
                raise FeatureProtocolError()
            return saved
        owner_id = await self.resolve(body.source)
        key = vk_key(owner_id, chat_id, thread_id)
        previous = await self.items.get(APPLICATION, key)
        if previous is not None:
            self._identity(previous)
            if (previous.value.created_by, previous.value.creation_request_id, previous.value.creation_fingerprint) != (
                user_id,
                str(body.request_id),
                fingerprint,
            ):
                raise Conflict()
            return previous
        value = VkDocument(
            id=uuid4(),
            created=datetime.now(UTC),
            owner_id=owner_id,
            chat_id=chat_id,
            thread_id=thread_id,
            title=body.title,
            last_post_id=0,
            with_reposts=body.with_reposts,
            with_header=body.with_header,
            include_keywords=body.include_keywords,
            exclude_keywords=body.exclude_keywords,
            is_suspended=True,
            created_by=user_id,
            updated_by=user_id,
            creation_request_id=str(body.request_id),
            creation_fingerprint=fingerprint,
        )
        tx = self.store.transaction("vk", APPLICATION, operation_id=uuid4().hex)
        tx.expect_absent("subscriptions", key)
        tx.expect_absent("requests", request_key)
        tx.put(self.items, key, value, parent=f"chat:{chat_id}:topic:{thread_id or 0}", status="paused")
        tx.put(self.requests, request_key, RepostRequest(target_key=key, fingerprint=fingerprint))
        try:
            result = await ApplicationDocuments._commit(tx)
        except Conflict:
            # Recheck a simultaneous create through the same identity guards.
            current = await self.items.get(APPLICATION, key)
            if current is not None and (
                current.value.created_by,
                current.value.creation_request_id,
                current.value.creation_fingerprint,
            ) == (user_id, str(body.request_id), fingerprint):
                return current
            raise
        return self.items.decode(result.records[0], APPLICATION)

    async def update(self, user_id: int, chat_id: int, thread_id: int | None, key: str, body: RepostUpdate) -> Record[VkDocument]:
        current = await self.items.get(APPLICATION, key)
        if current is None or (current.value.chat_id, current.value.thread_id) != (chat_id, thread_id):
            raise RepostError("Подписка не найдена в этой теме чата.")
        self._identity(current)
        if current.etag != str(body.etag):
            raise Conflict()
        value = current.value.model_copy(deep=True)
        for name in ("title", "with_reposts", "with_header", "include_keywords", "exclude_keywords", "archived"):
            if name in body.model_fields_set:
                setattr(value, name, getattr(body, name))
        value.is_suspended, value.updated_by = True, user_id
        tx = self.store.transaction("vk", APPLICATION, operation_id=uuid4().hex)
        tx.expect(current)
        tx.put(self.items, key, value, parent=f"chat:{chat_id}:topic:{thread_id or 0}", status="archived" if value.archived else "paused")
        result = await ApplicationDocuments._commit(tx)
        return self.items.decode(result.records[0], APPLICATION)

    async def preview(self, body: SourcePreview) -> dict[str, object]:
        owner_id = await self.resolve(body.source)
        base: dict[str, object] = {"owner_id": owner_id, "source_url": source_url(owner_id), "automatic_posting": False, "posts": []}
        if self.api is None:
            return base | {"available": False, "reason": "Доступ к VK не настроен. Подписку можно сохранить на паузе."}
        try:
            async with asyncio.timeout(6), self._providers:
                if not await public_source(self.api, owner_id):
                    return base | {"available": False, "reason": "Предпросмотр доступен только для подтверждённо открытых страниц VK."}
                raw: object = await self.api.request("wall.get", owner_id=owner_id, count=3, filter="all")
            wall = Wall.model_validate(raw)
            if len(wall.items) > 3 or any(post.owner_id != owner_id for post in wall.items):
                raise ValueError("Unexpected source")
        except Exception:
            return base | {"available": False, "reason": "VK не отдал стену. Проверь доступ к странице; подписка останется на паузе."}
        return base | {
            "available": True,
            "reason": "Предпросмотр последних трёх записей. Публикация отключена.",
            "posts": [
                {
                    "id": post.id,
                    "text": post.text[:4000],
                    "url": f"https://vk.com/wall{owner_id}_{post.id}",
                    "is_repost": bool(post.copy_history),
                    "selected": body.selects(post.text, bool(post.copy_history)),
                }
                for post in wall.items
                if not post.friends_only and not post.donut.is_donut
            ],
        }
