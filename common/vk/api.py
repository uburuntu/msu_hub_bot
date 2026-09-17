import asyncio
from functools import cached_property, partial
from operator import itemgetter
from typing import Dict, Final, Iterable, List, Tuple, Union

import aiohttp
from throttler import throttle

from msu_hub_bot.settings import MissingIntegration

from common import json
from common.utils import chunks, unique_by


class VkErrorBase(Exception):
    pass


class VkError(VkErrorBase):
    def __init__(self, method, params, error):
        self.method = method
        self.params = params
        self.error = error

    def __str__(self):
        return f"{self.__class__.__name__}: {self.method}: {self.error}"

    def __repr__(self):
        return str(self)


class VkErrorApi(VkError):
    def __init__(self, method, params, full_error):
        self.error_code = full_error["error_code"]
        self.full_error = full_error
        super().__init__(method, params, f"[{full_error['error_code']:>3}] {full_error['error_msg']}")


class VkApiCaller:
    api_url = "https://api.vk.com/method/"

    def __init__(self, token: str, version: str | None = None) -> None:
        self.token = token
        self.version = version or "5.124"

    @cached_property
    def session(self) -> aiohttp.ClientSession:
        return aiohttp.ClientSession()

    async def close(self) -> None:
        session = self.__dict__.get("session")
        if session is not None:
            await session.close()

    async def _request(self, method, **params):
        params["access_token"] = self.token
        if "v" not in params:
            params["v"] = self.version

        async with self.session.post(self.api_url + method, data=params) as response:
            if response.status == 200:
                result = json.loads(await response.read())
                if "error" in result:
                    raise VkErrorApi(method, params, result["error"])
                return result["response"]
            else:
                response.raise_for_status()

    @throttle(rate_limit=3, period=1.1)
    async def request(self, method, **params):
        if not self.token:
            raise MissingIntegration("vk_user_token")
        try:
            return await self._request(method, **params)
        except VkErrorApi as e:
            if e.error_code != 6:
                raise
            return await self.request(method, **params)


class VkApi(VkApiCaller):
    @staticmethod
    def extract_extended(d: dict) -> Dict[int, dict]:
        profiles, groups = d.get("profiles", []), d.get("groups", [])
        e = {}
        for p in profiles:
            e[p["id"]] = p
        for g in groups:
            e[-g["id"]] = g
        return e

    @staticmethod
    def _post_process_response(r: dict) -> dict:
        if items := r["items"]:
            if isinstance(items[0], dict):
                r["items"].sort(key=lambda x: x.get("date", 0))
        r["groups"] = unique_by(r["groups"], key=itemgetter("id"))
        r["profiles"] = unique_by(r["profiles"], key=itemgetter("id"))
        return r

    def _accumulate_responses(self, responses: List[dict], first_response: dict = None) -> dict:
        result = first_response or {}

        result.setdefault("items", [])
        result.setdefault("groups", [])
        result.setdefault("profiles", [])

        for r in responses:
            result["items"] += r.get("items", [])
            result["groups"] += r.get("groups", [])
            result["profiles"] += r.get("profiles", [])

        return self._post_process_response(result)

    def _items_and_extended(self, r: dict) -> Tuple[dict, dict]:
        return r.get("items", []), self.extract_extended(r)

    async def _offset_requests(self, method, max_count: int, count: int = None) -> dict:
        count = count or 2**32

        first_response = await method(offset=0, count=min(count, max_count))

        count = min(count, first_response["count"])
        coros = (method(offset=offset, count=min(count - offset, max_count)) for offset in range(max_count, count, max_count))
        responses = await asyncio.gather(*coros)
        return self._accumulate_responses(responses, first_response)

    async def get_wall(self, owner_id: int, count: int = None) -> Tuple[dict, dict]:
        method = partial(self.request, "wall.get", owner_id=owner_id, filter="all", extended=1)
        result = await self._offset_requests(method, max_count=100, count=count)
        return self._items_and_extended(result)

    async def get_wall_post(self, posts: str) -> Tuple[dict, dict]:
        result = await self.request("wall.getById", posts=posts, extended=1, copy_history_depth=1)
        return self._items_and_extended(result)

    async def get_group_members_ids(self, group_id: int) -> dict:
        method = partial(self.request, "groups.getMembers", group_id=group_id)
        result = await self._offset_requests(method, max_count=1000)
        return result["items"]

    async def get_group_members(self, group_id: int, fields: str = "screen_name,is_closed") -> dict:
        method = partial(self.request, "groups.getMembers", group_id=group_id, fields=fields)
        result = await self._offset_requests(method, max_count=1000)
        return result["items"]

    async def get_user_friends(self, user_id: int) -> dict:
        method = partial(self.request, "friends.get", user_id=user_id)
        result = await self._offset_requests(method, max_count=5000)
        return result["items"]

    async def get_users(self, user_ids: List[Union[int, str]], fields: str = "") -> List[dict]:
        _fields_full: Final = (
            "photo_id",
            "verified",
            "sex",
            "bdate",
            "city",
            "country",
            "home_town",
            "has_photo",
            "photo_50",
            "photo_100",
            "photo_200_orig",
            "photo_200",
            "photo_400_orig",
            "photo_max",
            "photo_max_orig",
            "online",
            "domain",
            "has_mobile",
            "contacts",
            "site",
            "education",
            "universities",
            "schools",
            "status",
            "last_seen",
            "followers_count",
            "common_count",
            "occupation",
            "nickname",
            "relatives",
            "relation",
            "personal",
            "connections",
            "exports",
            "activities",
            "interests",
            "music",
            "movies",
            "tv",
            "books",
            "games",
            "about",
            "quotes",
            "can_post",
            "can_see_all_posts",
            "can_see_audio",
            "can_write_private_message",
            "can_send_friend_request",
            "is_favorite",
            "is_hidden_from_feed",
            "timezone",
            "screen_name",
            "maiden_name",
            "crop_photo",
            "is_friend",
            "friend_status",
            "career",
            "military",
            "blacklisted",
            "blacklisted_by_me",
            "can_be_invited_group",
        )
        max_count: Final = 1000

        coros = []
        for ids_chunk in chunks(user_ids, max_count):
            coro = self.request("users.get", user_ids=",".join(map(str, ids_chunk)), fields=fields)
            coros.append(coro)

        result = []
        for coro in asyncio.as_completed(coros):
            result += await coro
        return result

    async def get_user_subscriptions(self, user_id: int) -> List[dict]:
        method = partial(self.request, "users.getSubscriptions", user_id=user_id, extended=1)
        result = await self._offset_requests(method, max_count=200)
        return result["items"]

    async def get_user_search(self, **params) -> List[dict]:
        method = partial(self.request, "users.search", **params)
        result = await self._offset_requests(method, max_count=1000, count=1000)
        return result["items"]

    async def get_groups(self, group_ids: List[Union[int, str]], fields: str = "") -> List[dict]:
        _fields_full: Final = (
            "city",
            "country",
            "place",
            "description",
            "wiki_page",
            "market",
            "members_count",
            "counters",
            "start_date",
            "finish_date",
            "can_post",
            "can_see_all_posts",
            "activity",
            "status",
            "contacts",
            "links",
            "fixed_post",
            "verified",
            "site",
            "ban_info",
            "cover",
        )
        max_count: Final = 500

        coros = []
        for ids_chunk in chunks(group_ids, max_count):
            coro = self.request("groups.getById", group_ids=",".join(map(str, ids_chunk)), fields=fields)
            coros.append(coro)

        result = []
        for coro in asyncio.as_completed(coros):
            result += await coro
        return result

    async def get_newsfeed(self, owner_ids: Iterable[int], from_ts: int = 0) -> Tuple[dict, dict]:
        """Get posts from last 10 days"""
        count: Final[int] = 100

        source_ids = ",".join(map(str, owner_ids))
        method = partial(
            self.request, "newsfeed.get", filters="post", return_banned=1, start_time=from_ts, source_ids=source_ids, count=count
        )

        result = r = await method()
        while next_from := r.get("next_from"):
            r = await method(start_from=next_from)
            result["items"] += r["items"]
            result["groups"] += r["groups"]
            result["profiles"] += r["profiles"]

        result = self._post_process_response(result)
        return self._items_and_extended(result)
