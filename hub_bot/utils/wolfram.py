from msu_hub_bot.settings import MissingIntegration

from contextlib import suppress
from functools import cached_property
from io import BytesIO
from typing import Tuple

import aiogram
import aiohttp
from PIL import Image
from aiogram.types import InputFile, Message
from aiogram.utils.markdown import hcode

from common.utils import valid_filename, image_bytes_io


class WolframAPIError(Exception):
    pass


class WolframAPI:
    """
    Docs: https://products.wolframalpha.com/simple-api/documentation/
    """
    api_url = 'https://api.wolframalpha.com/v1/simple'

    def __init__(self, token: str):
        self.token = token

    @cached_property
    def session(self) -> aiohttp.ClientSession:
        return aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))

    async def close(self):
        session = self.__dict__.get('session')
        if session is not None:
            await session.close()

    async def _request(self, **params) -> bytes:
        params = {
            'appid': self.token,
            'layout': 'labelbar',
            'width': 600,
            'units': 'metric',
            'timeout': 8,
            **params,
        }
        async with self.session.get(self.api_url, params=params) as response:
            if response.status == 200:
                result = await response.read()
                return result
            else:
                raise WolframAPIError(f'[{response.status}] {response.reason}')

    async def request(self, query: str) -> Tuple[BytesIO, float]:
        content = await self._request(i=query)

        def crop(img: Image, from_top: int = 0, from_bottom: int = 0) -> Image:
            box = (0, from_top, img.width, img.height - from_bottom)
            return img.crop(box)

        image = crop(Image.open(BytesIO(content)), from_top=75, from_bottom=45)
        return image_bytes_io(image, f'wolfram_{valid_filename(query)}', 'png'), image.height / image.width

    async def process_wolfram(self, message: Message):
        if not self.token:
            raise MissingIntegration("wolfram_token")

        query = message.get_args()
        if not query:
            if reply_to := message.reply_to_message:
                query = reply_to.text or reply_to.caption

        if not query:
            return await message.reply('Использование: ' + hcode('/wf sum 1/n^2, n=1..inf'))

        target = await message.reply('🔄 WolframAlpha обрабатывает запрос…')

        try:
            try:
                image, ratio = await self.request(query=query)
            except (WolframAPIError, aiohttp.ClientError, TimeoutError):
                return await message.reply('Не удалось получить результат от WolframAlpha. Попробуйте другой запрос или повторите позже.')

            if ratio > 2.1:
                return await message.reply_document(InputFile(image))
            return await message.reply_photo(InputFile(image))
        finally:
            with suppress(aiogram.exceptions.TelegramAPIError, aiohttp.ClientError, TimeoutError):
                await target.delete()
