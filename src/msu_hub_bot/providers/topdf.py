import asyncio
import io
import random
import time
from typing import Tuple
from urllib.parse import unquote

import aiohttp

from msu_hub_bot.providers.exceptions import BadRequestError

JOB_TIMEOUT_SECONDS = 180


def _conversion_id() -> str:
    """Match the uploader's base-32 timestamp, five random chunks and counter."""
    alphabet = "0123456789abcdefghijklmnopqrstuv"
    values = [int(time.time() * 1000), *(int(65535 * random.random()) for _ in range(5))]
    chunks = []
    for value in values:
        digits = ""
        while value:
            value, remainder = divmod(value, 32)
            digits = alphabet[remainder] + digits
        chunks.append(digits or "0")
    # The uploader initializes its counter inside each call, so it is always zero.
    return "o_" + "".join(chunks) + "0"


async def _response_json(response) -> dict:
    if response.status != 200:
        raise BadRequestError()
    try:
        result = await response.json()
        if not isinstance(result, dict):
            raise TypeError
        return result
    except (aiohttp.ContentTypeError, TypeError, ValueError):
        raise BadRequestError() from None


async def convert_to_pdf(file: io.BytesIO, filename: str, content_type: str) -> Tuple[str, str, str]:
    sid = "".join(random.choices("0123456789abcdefghiklmnopqrstuvwxyz", k=16))
    fid = _conversion_id()

    data = aiohttp.formdata.FormData()
    data.add_field("name", filename)
    data.add_field("id", fid)
    data.add_field("file", file, content_type=content_type, filename=filename)

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:82.0) Gecko/20100101 Firefox/82.0",
        "referrer": "https://topdf.com/",
    }

    async with (
        asyncio.timeout(JOB_TIMEOUT_SECONDS),
        aiohttp.ClientSession(headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as session,
    ):
        url = f"https://topdf.com/upload/{sid}"
        async with session.post(url, data=data, allow_redirects=False) as response:
            result = await _response_json(response)

        url = f"https://topdf.com/convert/{sid}/{fid}?rnd={random.random()}"
        async with session.get(url, headers={"X-Requested-With": "XMLHttpRequest"}, allow_redirects=False) as response:
            result = await _response_json(response)

        while True:
            await asyncio.sleep(1)
            url = f"https://topdf.com/status/{sid}/{fid}?rnd={random.random()}"
            async with session.get(url, headers={"X-Requested-With": "XMLHttpRequest"}, allow_redirects=False) as response:
                result = await _response_json(response)
                if result.get("status") != "processing":
                    break

    if not all(isinstance(result.get(key), str) and result[key] for key in ("convert_result", "thumb_url")):
        raise BadRequestError()

    convert_name = result["convert_result"]
    thumb = "https://topdf.com/" + result["thumb_url"]
    return f"https://topdf.com/download/{sid}/{fid}/{convert_name}", thumb, unquote(convert_name)
