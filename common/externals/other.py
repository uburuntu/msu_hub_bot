from msu_hub_bot.settings import settings

import asyncio
import io
import re
from typing import Union

import aiohttp

from common import json
from common.externals.exceptions import BadRequestError
from common.utils import bytes_io

UPLOAD_TIMEOUT_SECONDS = 180


async def duckduckgo(query: str) -> dict:
    headers = {
        'Accept-Language': 'ru-RU,ru;q=0.8,en-US;q=0.5,en;q=0.3',
    }

    async with aiohttp.ClientSession(headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as session:
        url = f'https://api.duckduckgo.com/'
        async with session.get(url, params=dict(q=query, format='json', no_redirect=1, t='https://t.me/msu_hub_bot')) as response:
            if response.status != 200:
                raise BadRequestError()
            try:
                result = json.loads(await response.read())
                keys = ('Redirect', 'Heading', 'AbstractText', 'AbstractURL', 'Image')
                if not isinstance(result, dict) or not all(isinstance(result.get(key, ''), str) for key in keys):
                    raise ValueError
                return {key: result.get(key, '') for key in keys}
            except (TypeError, ValueError):
                raise BadRequestError() from None


async def remove_bg(file: io.BytesIO) -> str:
    data = aiohttp.formdata.FormData()
    data.add_field('source_image_file', file, content_type='image/jpeg', filename='bg.jpg')

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:89.0) Gecko/20100101 Firefox/89.0',
    }

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        async with session.get('https://www.slazzer.com/upload') as response:
            if response.status != 200:
                raise BadRequestError()
            result = await response.text()
            csrf = re.search(r'<meta name="csrf-token" content="(\S+)">', result)
            if csrf is None:
                raise BadRequestError()

        headers['X-CSRFToken'] = csrf.group(1)
        headers['X-Requested-With'] = 'XMLHttpRequest'
        headers['Referer'] = 'https://www.slazzer.com/upload'

        async with session.post('https://www.slazzer.com/upload_image', headers=headers, data=data) as response:
            if response.status != 200:
                raise BadRequestError()
            result = await response.read()

    try:
        result = json.loads(result)
        preview = result['preview_size_output_image']
        if not isinstance(preview, str) or not preview.startswith('/') or preview.startswith('//'):
            raise ValueError
        return 'https://slazzer.com' + preview
    except (KeyError, TypeError, ValueError):
        raise BadRequestError() from None


async def remove_bg_api(file: Union[io.BytesIO, str]) -> io.BytesIO:
    data = aiohttp.formdata.FormData()
    if isinstance(file, str):
        data.add_field('source_image_url', file)
    else:
        data.add_field('source_image_file', file, content_type='image/jpeg', filename='bg.jpg')

    data.add_field('crop', 'true')
    data.add_field('preview', 'true')

    headers = {
        'API-KEY': settings.require('remove_bg_api_key'),
    }

    async with aiohttp.ClientSession(headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as session:
        async with session.post('https://api.slazzer.com/v2.0/remove_image_background', data=data) as response:
            if response.status != 200:
                raise BadRequestError()
            result = await response.read()

    return bytes_io(result, filename='removed_bg.png')


async def imgur_upload(file: io.BytesIO, image_or_video: str = 'image') -> dict:
    # Docs: https://apidocs.imgur.com/#c85c9dfc-7487-4de2-9ecd-66f727cf3139

    data = aiohttp.formdata.FormData()
    data.add_field(image_or_video, file)

    async def upload_result(response):
        if response.status != 200:
            raise BadRequestError()
        try:
            payload = json.loads(await response.read())
            result = payload['data']
            if payload.get('success') is False or not isinstance(result, dict) or result.get('error'):
                raise ValueError
            if result.get('processing') is not None and not isinstance(result['processing'], dict):
                raise ValueError
            return result
        except (KeyError, TypeError, ValueError):
            raise BadRequestError() from None

    async with asyncio.timeout(UPLOAD_TIMEOUT_SECONDS), aiohttp.ClientSession(
        headers={'Authorization': settings.require('imgur_authorization')}, timeout=aiohttp.ClientTimeout(total=30)
    ) as session:
        async with session.post('https://api.imgur.com/3/upload', data=data) as response:
            result = await upload_result(response)

        while (result.get('processing') or {}).get('status') in ('pending', 'started'):
            if not isinstance(result.get('id'), str):
                raise BadRequestError()
            await asyncio.sleep(1)

            async with session.get('https://api.imgur.com/3/image/' + result['id']) as response:
                result = await upload_result(response)

    if not all(key in result for key in ('link', 'width', 'height', 'size')) or not isinstance(result['link'], str):
        raise BadRequestError()
    return result


async def porfirevich(text: str) -> str:
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:80.0) Gecko/20100101 Firefox/80.0',
    }
    data = {
        'prompt': text, 'length': 60,
    }

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        url = 'https://pelevin.gpt.dobro.ai/generate/'
        async with session.post(url, headers=headers, json=data) as response:
            if response.status != 200:
                raise BadRequestError()
            try:
                result = await response.json()
                replies = result['replies']
                if not isinstance(replies, list) or not replies or not isinstance(replies[-1], str) or not replies[-1].strip():
                    raise ValueError
                return replies[-1]
            except (aiohttp.ContentTypeError, KeyError, TypeError, ValueError):
                raise BadRequestError() from None
