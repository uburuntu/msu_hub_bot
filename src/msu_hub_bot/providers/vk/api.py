"""Bounded VK API requests and public-only wall reads."""

import asyncio
import json
import re
from collections import deque
from functools import cached_property
from time import monotonic
from typing import Protocol

import aiohttp
from pydantic import TypeAdapter, ValidationError

from msu_hub_bot.providers.exceptions import ExternalServiceError
from msu_hub_bot.providers.vk.models import Entity, Post, Source, Wall
from msu_hub_bot.settings import MissingIntegration

API_VERSION = "5.199"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
REQUEST_TIMEOUT = 15
MAX_ATTEMPTS = 3
POST_IDS = re.compile(r"-?[1-9][0-9]*_[1-9][0-9]*(?:,-?[1-9][0-9]*_[1-9][0-9]*){0,9}")


class VkErrorBase(ExternalServiceError):
    def __init__(self) -> None:
        super().__init__("VK не ответил или не открыл запись. Попробуй ссылку на общедоступный пост чуть позже.")


class VkError(VkErrorBase):
    pass


class VkErrorApi(VkError):
    """Only the numeric category is retained; VK error bodies echo credentials."""

    def __init__(self, error_code: int) -> None:
        self.error_code = error_code
        super().__init__()


class Requester(Protocol):
    async def request(self, method: str, **params: str | int) -> object: ...


async def public_source(api: Requester, owner_id: int) -> bool:
    if owner_id < 0:
        raw = await api.request("groups.getById", group_ids=str(-owner_id))
        if isinstance(raw, dict):
            raw = raw.get("groups")
    else:
        raw = await api.request("users.get", user_ids=str(owner_id))
    sources = TypeAdapter(list[Source]).validate_python(raw)
    return len(sources) == 1 and sources[0].id == abs(owner_id) and sources[0].is_closed == 0 and not sources[0].deactivated


class VkApiCaller:
    api_url = "https://api.vk.com/method/"

    def __init__(self, token: str, version: str | None = None) -> None:
        self.token = token
        self.version = version or API_VERSION
        self._rate_lock = asyncio.Lock()
        self._starts: deque[float] = deque()

    @cached_property
    def session(self) -> aiohttp.ClientSession:
        return aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10, connect=4))

    async def close(self) -> None:
        session = self.__dict__.get("session")
        if session is not None:
            await session.close()

    async def _rate_limit(self) -> None:
        async with self._rate_lock:
            now = monotonic()
            while self._starts and self._starts[0] <= now - 1.1:
                self._starts.popleft()
            if len(self._starts) >= 3:
                await asyncio.sleep(max(0, self._starts[0] + 1.1 - now))
                self._starts.popleft()
            self._starts.append(monotonic())

    async def _request(self, method: str, **params: str | int) -> object:
        data = params | {"access_token": self.token, "v": self.version}
        async with self.session.post(self.api_url + method, data=data, allow_redirects=False) as response:
            if response.status != 200 or (response.content_length or 0) > MAX_RESPONSE_BYTES:
                raise VkError()
            body = bytearray()
            async for chunk in response.content.iter_chunked(64 * 1024):
                body.extend(chunk)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise VkError()
            result: object = json.loads(body)
            if not isinstance(result, dict):
                raise VkError()
            if error := result.get("error"):
                code = error.get("error_code") if isinstance(error, dict) else None
                raise VkErrorApi(code if type(code) is int else 0)
            if "response" not in result:
                raise VkError()
            return result["response"]

    async def request(self, method: str, **params: str | int) -> object:
        if not self.token:
            raise MissingIntegration("vk_user_token")
        if not re.fullmatch(r"[a-zA-Z]+\.[a-zA-Z]+", method) or {"access_token", "v"} & params.keys():
            raise VkError()
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT):
                for attempt in range(MAX_ATTEMPTS):
                    await self._rate_limit()
                    try:
                        return await self._request(method, **params)
                    except VkErrorApi as error:
                        if error.error_code != 6 or attempt == MAX_ATTEMPTS - 1:
                            raise
                        await asyncio.sleep(1.1 * (attempt + 1))
        except aiohttp.ClientError, TimeoutError, ValueError, RecursionError:
            raise VkError() from None
        raise VkError()


class VkApi(VkApiCaller):
    async def _public_wall(self, raw: object, owners: set[int]) -> Wall:
        wall = Wall.model_validate(raw)
        if any(post.owner_id not in owners for post in wall.items):
            raise VkError()
        checked = dict.fromkeys(owners, True)

        async def accessible(post: Post, depth: int = 0) -> bool:
            if depth > 2 or not post.is_public:
                return False
            if post.owner_id not in checked:
                checked[post.owner_id] = await public_source(self, post.owner_id)
            if not checked[post.owner_id]:
                return False
            for copy in post.copy_history:
                if not await accessible(copy, depth + 1):
                    return False
            return True

        wall.items = [post for post in wall.items if await accessible(post)]
        return wall

    async def get_wall(self, owner_id: int, count: int | None = None) -> tuple[list[Post], dict[int, Entity]]:
        count = 100 if count is None else count
        if not owner_id or not 1 <= count <= 100:
            raise VkError()
        try:
            async with asyncio.timeout(30):
                if not await public_source(self, owner_id):
                    raise VkError()
                raw = await self.request("wall.get", owner_id=owner_id, count=count, filter="all", extended=1)
                wall = await self._public_wall(raw, {owner_id})
                return wall.items, wall.extended
        except ValidationError, TimeoutError:
            raise VkError() from None

    async def get_wall_post(self, posts: str) -> tuple[list[Post], dict[int, Entity]]:
        if not POST_IDS.fullmatch(posts) or len(posts) > 500:
            raise VkError()
        identifiers = {tuple(map(int, post.split("_"))) for post in posts.split(",")}
        owners = {owner for owner, _ in identifiers}
        try:
            async with asyncio.timeout(30):
                for owner_id in owners:
                    if not await public_source(self, owner_id):
                        raise VkError()
                raw = await self.request("wall.getById", posts=posts, extended=1, copy_history_depth=2)
                wall = await self._public_wall(raw, owners)
                if any((post.owner_id, post.id) not in identifiers for post in wall.items):
                    raise VkError()
                return wall.items, wall.extended
        except ValidationError, TimeoutError:
            raise VkError() from None
