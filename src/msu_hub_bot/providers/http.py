"""Small, bounded response readers for public provider protocols."""

import json

import aiohttp

from msu_hub_bot.providers.exceptions import BadRequestError

USER_AGENT = "MSUHubBot/1.0 (https://github.com/uburuntu/msu_hub_bot)"
MAX_JSON_BYTES = 1024 * 1024


async def read_limited(response: aiohttp.ClientResponse, limit: int) -> bytes:
    if response.status != 200 or (response.content_length is not None and response.content_length > limit):
        raise BadRequestError()
    result = bytearray()
    async for chunk in response.content.iter_chunked(65536):
        result.extend(chunk)
        if len(result) > limit:
            raise BadRequestError()
    return bytes(result)


async def read_json(response: aiohttp.ClientResponse) -> object:
    try:
        return json.loads(await read_limited(response, MAX_JSON_BYTES))
    except ValueError, UnicodeError:
        raise BadRequestError() from None
