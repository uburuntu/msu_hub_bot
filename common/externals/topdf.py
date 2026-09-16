import asyncio
import io
import random
from typing import Tuple
from urllib.parse import unquote

import aiohttp
import js2py

from common.externals.exceptions import BadRequestError

JOB_TIMEOUT_SECONDS = 180

js_rand = js2py.eval_js(
    'function () {\n'
    '    var e = 0;\n'
    '    n = (new Date).getTime().toString(32);\n'
    '    for (i = 0; 5 > i; i++) n += Math.floor(65535 * Math.random()).toString(32);\n'
    '    return \'o_\' + n + (e++).toString(32)\n'
    '};'
)


async def convert_to_pdf(file: io.BytesIO, filename: str, content_type: str) -> Tuple[str, str, str]:
    sid = ''.join(random.choices('0123456789abcdefghiklmnopqrstuvwxyz', k=16))
    fid = js_rand()

    data = aiohttp.formdata.FormData()
    data.add_field('name', filename)
    data.add_field('id', fid)
    data.add_field('file', file, content_type=content_type, filename=filename)

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:82.0) Gecko/20100101 Firefox/82.0',
        'referrer': 'https://topdf.com/',
    }

    async with asyncio.timeout(JOB_TIMEOUT_SECONDS), aiohttp.ClientSession(headers=headers) as session:
        url = f'https://topdf.com/upload/{sid}'
        async with session.post(url, data=data) as response:
            if response.status != 200:
                raise BadRequestError()
            result = await response.json()

        url = f'https://topdf.com/convert/{sid}/{fid}?rnd={random.random()}'
        async with session.get(url, headers={'X-Requested-With': 'XMLHttpRequest'}) as response:
            if response.status != 200:
                raise BadRequestError()
            result = await response.json()

        while True:
            await asyncio.sleep(1)
            url = f'https://topdf.com/status/{sid}/{fid}?rnd={random.random()}'
            async with session.get(url, headers={'X-Requested-With': 'XMLHttpRequest'}) as response:
                if response.status != 200:
                    raise BadRequestError()
                result = await response.json()
                if result['status'] != 'processing':
                    break

    if 'convert_result' not in result:
        raise BadRequestError()

    convert_name = result['convert_result']
    thumb = 'https://topdf.com/' + result['thumb_url']
    return f'https://topdf.com/download/{sid}/{fid}/{convert_name}', thumb, unquote(convert_name)
