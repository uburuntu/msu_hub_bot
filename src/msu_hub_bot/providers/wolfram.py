from msu_hub_bot.settings import MissingIntegration
from msu_hub_bot.telemetry import Boundary, Provider, Telemetry

from contextlib import suppress
from functools import cached_property
from io import BytesIO
from typing import Tuple

from aiogram.exceptions import TelegramAPIError
import aiohttp
from PIL import Image
from aiogram.types import Message
from aiogram.utils.markdown import hcode

from msu_hub_bot.utils import valid_filename, image_bytes_io
from msu_hub_bot.telegram.files import input_file
from msu_hub_bot.telegram.utils import command_arguments


class WolframAPIError(Exception):
    pass


class WolframAPI:
    """
    Docs: https://products.wolframalpha.com/simple-api/documentation/
    """

    api_url = "https://api.wolframalpha.com/v1/simple"

    def __init__(self, token: str, *, telemetry: Telemetry | None = None) -> None:
        self.token = token
        self.telemetry = telemetry or Telemetry()

    @cached_property
    def session(self) -> aiohttp.ClientSession:
        return aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))

    async def close(self) -> None:
        session = self.__dict__.get("session")
        if session is not None:
            await session.close()

    async def _request(self, **params: str) -> bytes:
        with self.telemetry.operation(Boundary.PROVIDER, "wolfram.query", provider=Provider.WOLFRAM):
            return await self._request_raw(**params)

    async def _request_raw(self, **params: str) -> bytes:
        request_params: dict[str, str | int] = {
            "appid": self.token,
            "layout": "labelbar",
            "width": 600,
            "units": "metric",
            "timeout": 8,
            **params,
        }
        async with self.session.get(self.api_url, params=request_params) as response:
            if response.status == 200:
                result = await response.read()
                return result
            else:
                raise WolframAPIError(f"[{response.status}] {response.reason}")

    async def request(self, query: str) -> Tuple[BytesIO, float]:
        content = await self._request(i=query)

        def crop(img: Image.Image, from_top: int = 0, from_bottom: int = 0) -> Image.Image:
            box = (0, from_top, img.width, img.height - from_bottom)
            return img.crop(box)

        image = crop(Image.open(BytesIO(content)), from_top=75, from_bottom=45)
        return image_bytes_io(image, f"wolfram_{valid_filename(query)}", "png"), image.height / image.width

    async def process_wolfram(self, message: Message) -> Message:
        if not self.token:
            raise MissingIntegration("wolfram_token")

        query = command_arguments(message)
        if not query:
            if reply_to := message.reply_to_message:
                query = reply_to.text or reply_to.caption or ""

        if not query:
            return await message.reply("Использование: " + hcode("/wf sum 1/n^2, n=1..inf"))

        target = await message.reply("🔄 WolframAlpha обрабатывает запрос…")

        try:
            try:
                image, ratio = await self.request(query=query)
            except WolframAPIError, aiohttp.ClientError, TimeoutError:
                return await message.reply("Не удалось получить результат от WolframAlpha. Попробуйте другой запрос или повторите позже.")

            if ratio > 2.1:
                return await message.reply_document(input_file(image, "wolfram.png"))
            return await message.reply_photo(input_file(image, "wolfram.png"))
        finally:
            with suppress(TelegramAPIError, aiohttp.ClientError, TimeoutError):
                await target.delete()
