from msu_hub_bot.settings import settings

import io

import aiohttp
from aiocache import cached

from common.externals.exceptions import BadRequestError

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


async def translate(text: str, src: str, dest: str) -> str:
    data = {
        "from": await full_code(src),
        "to": await full_code(dest),
        "text": text,
        "platform": "dp",
    }

    async with aiohttp.ClientSession() as session:
        url = "https://backenster.com/b1/api/v3/translate/"
        async with session.post(url, headers=headers, json=data) as response:
            if response.status != 200:
                raise BadRequestError()
            result = await response.json()

    if result.get("err") is not None or "result" not in result:
        raise BadRequestError()

    return result["result"]


async def translate_image(file: io.BytesIO, src: str, dest: str) -> str:
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
