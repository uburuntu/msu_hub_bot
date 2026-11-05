from msu_hub_bot.settings import settings

import io
from dataclasses import dataclass

import aiohttp
from aiocache import cached

from msu_hub_bot.providers.exceptions import BadRequestError

# Docs:
# - https://lingvanex.com/demo/
# - https://lingvanex.com/lingvanex_demo_page/js/api-base.js
# - https://lingvanex.com/lingvanex_demo_page/js/translateImage.js

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:80.0) Gecko/20100101 Firefox/80.0",
    "Authorization": settings.lingvanex_authorization,
    "Accept-Language": "ru-RU,ru;q=0.8,en-US;q=0.5,en;q=0.3",
    "Content-type": "application/json; charset=UTF-8",
    "referrer": "https://lingvanex.com/lingvanex_demo_page/translateText.html",
}


@cached(ttl=10 * 60)
async def languages_list() -> dict:
    settings.require("lingvanex_authorization")
    async with aiohttp.ClientSession() as session:
        url = "https://backenster.com/b1/api/v3/getLanguages/"
        async with session.get(url, headers=headers, params=dict(platform="dp")) as response:
            if response.status != 200:
                raise BadRequestError()
            result = await response.json()

    if result.get("err") is not None or "result" not in result:
        raise BadRequestError()

    return result["result"]


@cached()
async def full_code(code: str) -> str:
    langs = await languages_list()
    for lang in langs:
        if code in (lang["code_alpha_1"], lang["full_code"], lang["codeName"]):
            return lang["full_code"]
    raise BadRequestError()


@dataclass(frozen=True)
class Translation:
    text: str
    # The language Lingvanex translated from: the requested one or its own detection.
    source: str | None = None


async def is_language(code: str) -> bool:
    """A known Lingvanex code; an unavailable catalogue means no argument is a code."""
    try:
        await full_code(code)
    except BadRequestError, aiohttp.ClientError, TimeoutError:
        return False
    return True


def same_language(first: str | None, second: str | None) -> bool:
    return bool(first and second) and first.replace("-", "_").split("_")[0].lower() == second.replace("-", "_").split("_")[0].lower()


async def translate(text: str, src: str | None, dest: str) -> Translation:
    """Without ``src``, Lingvanex detects the text language and reports it."""
    data = {"to": await full_code(dest), "text": text, "platform": "dp"}
    if src is not None:
        data["from"] = await full_code(src)

    async with aiohttp.ClientSession() as session:
        url = "https://backenster.com/b1/api/v3/translate/"
        async with session.post(url, headers=headers, json=data) as response:
            if response.status != 200:
                raise BadRequestError()
            result = await response.json()

    if result.get("err") is not None or not isinstance(result.get("result"), str):
        raise BadRequestError()

    source = result.get("from")
    return Translation(result["result"], source if isinstance(source, str) else src)


async def translate_image(file: io.BytesIO, src: str, dest: str) -> str:
    """Image translation has no detection; an unknown source garbles the result."""
    data = aiohttp.formdata.FormData()
    data.add_field("from", await full_code(src))
    data.add_field("to", await full_code(dest))
    data.add_field("file", file, content_type="image/jpeg", filename="file.jpg")

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:84.0) Gecko/20100101 Firefox/84.0",
        "Accept-Language": "ru-RU,ru;q=0.8,en-US;q=0.5,en;q=0.3",
        "Authorization": settings.require("lingvanex_image_authorization"),
        "referrer": "https://lingvanex.com/lingvanex_demo_page/translateImage.html",
    }

    async with aiohttp.ClientSession() as session:
        url = "https://backenster.com/v2/api/v3/parseImage?platform=dp"
        async with session.post(url, headers=headers, data=data) as response:
            if response.status != 200:
                raise BadRequestError()
            result = await response.json()

    if result.get("err") is not None or "translatedData" not in result:
        raise BadRequestError()

    return "\n".join(result["translatedData"])
