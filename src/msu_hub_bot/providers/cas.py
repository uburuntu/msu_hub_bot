import aiohttp
from aiocache import cached

from msu_hub_bot import json


class CombotAntiSpam:
    api_url = "https://api.cas.chat/"

    @classmethod
    async def _request(cls, path: str, **params) -> dict:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            async with session.get(cls.api_url + path, params=params) as response:
                if response.status != 200:
                    print("CAS: user check failed", response.status, response.reason)
                    return {}
                result = json.loads(await response.read())
                return result

    @classmethod
    @cached(ttl=10 * 60, noself=True)
    async def banned(cls, user_id: int) -> bool:
        resp = await cls._request("check", user_id=user_id)
        return bool(resp.get("ok", False))
