from msu_hub_bot.settings import settings

import asyncio
import io

import aiohttp

from msu_hub_bot import json
from msu_hub_bot.providers.exceptions import BadRequestError

UPLOAD_TIMEOUT_SECONDS = 180


async def duckduckgo(query: str) -> dict[str, str]:
    from msu_hub_bot.providers.knowledge import answer

    return dict(await answer(query))


async def imgur_upload(file: io.BytesIO, image_or_video: str = "image") -> dict:
    # Docs: https://apidocs.imgur.com/#c85c9dfc-7487-4de2-9ecd-66f727cf3139

    data = aiohttp.formdata.FormData()
    data.add_field(image_or_video, file)

    async def upload_result(response):
        if response.status != 200:
            raise BadRequestError()
        try:
            payload = json.loads(await response.read())
            result = payload["data"]
            if payload.get("success") is False or not isinstance(result, dict) or result.get("error"):
                raise ValueError
            if result.get("processing") is not None and not isinstance(result["processing"], dict):
                raise ValueError
            return result
        except KeyError, TypeError, ValueError:
            raise BadRequestError() from None

    async with (
        asyncio.timeout(UPLOAD_TIMEOUT_SECONDS),
        aiohttp.ClientSession(
            headers={"Authorization": settings.require("imgur_authorization")}, timeout=aiohttp.ClientTimeout(total=30)
        ) as session,
    ):
        async with session.post("https://api.imgur.com/3/upload", data=data) as response:
            result = await upload_result(response)

        while (result.get("processing") or {}).get("status") in ("pending", "started"):
            if not isinstance(result.get("id"), str):
                raise BadRequestError()
            await asyncio.sleep(1)

            async with session.get("https://api.imgur.com/3/image/" + result["id"]) as response:
                result = await upload_result(response)

    if not all(key in result for key in ("link", "width", "height", "size")) or not isinstance(result["link"], str):
        raise BadRequestError()
    return result


async def porfirevich(text: str) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:80.0) Gecko/20100101 Firefox/80.0",
    }
    data = {
        "prompt": text,
        "length": 60,
    }

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        url = "https://pelevin.gpt.dobro.ai/generate/"
        async with session.post(url, headers=headers, json=data) as response:
            if response.status != 200:
                raise BadRequestError()
            try:
                result = await response.json()
                replies = result["replies"]
                if not isinstance(replies, list) or not replies or not isinstance(replies[-1], str) or not replies[-1].strip():
                    raise ValueError
                return replies[-1]
            except aiohttp.ContentTypeError, KeyError, TypeError, ValueError:
                raise BadRequestError() from None
