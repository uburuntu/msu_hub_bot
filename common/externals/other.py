from msu_hub_bot.settings import settings

import asyncio
import io
import re
from typing import Union

import aiohttp

from common import json
from common.externals.exceptions import BadRequestError
from common.utils import bytes_io


async def duckduckgo(query: str) -> dict:
    headers = {
        'Accept-Language': 'ru-RU,ru;q=0.8,en-US;q=0.5,en;q=0.3',
    }

    async with aiohttp.ClientSession(headers=headers) as session:
        url = f'https://api.duckduckgo.com/'
        async with session.get(url, params=dict(q=query, format='json', no_redirect=1, t='https://t.me/msu_hub_bot')) as response:
            if response.status != 200:
                raise BadRequestError()
            result = json.loads(await response.read())

    return result


async def remove_bg(file: io.BytesIO) -> str:
    data = aiohttp.formdata.FormData()
    data.add_field('source_image_file', file, content_type='image/jpeg', filename='bg.jpg')

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:89.0) Gecko/20100101 Firefox/89.0',
    }

    async with aiohttp.ClientSession() as session:
        async with session.get('https://www.slazzer.com/upload') as response:
            if response.status != 200:
                raise BadRequestError()
            result = await response.text()
            csrf = re.findall(r'<meta name="csrf-token" content="(\S+)">', result)[0]

        headers['X-CSRFToken'] = csrf
        headers['X-Requested-With'] = 'XMLHttpRequest'
        headers['Referer'] = 'https://www.slazzer.com/upload'

        async with session.post('https://www.slazzer.com/upload_image', headers=headers, data=data) as response:
            if response.status != 200:
                raise BadRequestError()
            result = await response.read()

    result = json.loads(result)
    return 'https://slazzer.com' + result['preview_size_output_image']


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

    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.post('https://api.slazzer.com/v2.0/remove_image_background', data=data) as response:
            # if response.status != 200:
            #     raise BadRequestError()
            print(response.status, response.reason, await response.text())
            response.raise_for_status()
            result = await response.read()

    return bytes_io(result, filename='removed_bg.png')


async def imgur_upload(file: io.BytesIO, image_or_video: str = 'image') -> dict:
    # Docs: https://apidocs.imgur.com/#c85c9dfc-7487-4de2-9ecd-66f727cf3139

    data = aiohttp.formdata.FormData()
    data.add_field(image_or_video, file)

    async with aiohttp.ClientSession(headers={'Authorization': settings.require('imgur_authorization')}) as session:
        async with session.post('https://api.imgur.com/3/upload', data=data) as response:
            result = json.loads(await response.read())['data']

        while result.get('processing', {}).get('status') in ('pending', 'started'):
            await asyncio.sleep(1)

            async with session.get('https://api.imgur.com/3/image/' + result['id']) as response:
                result = json.loads(await response.read())['data']

    return result


async def porfirevich(text: str) -> str:
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:80.0) Gecko/20100101 Firefox/80.0',
    }
    data = {
        'prompt': text, 'length': 60,
    }

    async with aiohttp.ClientSession() as session:
        url = 'https://pelevin.gpt.dobro.ai/generate/'
        async with session.post(url, headers=headers, data=json.dumps(data)) as response:
            if response.status != 200:
                raise BadRequestError()
            result = await response.json()

    return result['replies'][-1]
