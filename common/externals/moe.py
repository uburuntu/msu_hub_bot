import io
from typing import Union

import aiohttp

from common import json
from common.externals.exceptions import BadRequestError


async def which_anime(file: Union[io.BytesIO, str]) -> dict:
    data = aiohttp.formdata.FormData()
    params = {}

    if isinstance(file, str):
        method = "GET"
        params["url"] = file
    else:
        method = "POST"
        data.add_field("image", file)

    async with aiohttp.ClientSession() as session:
        url = "https://api.trace.moe/search"
        async with session.request(method, url, data=data, params=params) as response:
            if response.status != 200:
                raise BadRequestError()
            result = await response.json(loads=json.loads)

    return result
