from functools import cached_property
from urllib.parse import quote

import aiohttp


class Codecogs:
    api_url = "https://latex.codecogs.com/"

    @cached_property
    def session(self) -> aiohttp.ClientSession:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:81.0) Gecko/20100101 Firefox/81.0",
            "referrer": "https://www.codecogs.com/latex/eqneditor.php?lang=en-en",
        }
        return aiohttp.ClientSession(headers=headers)

    async def close(self):
        await self.session.close()

    @classmethod
    def url(cls, code: str) -> str:
        return cls.api_url + r"png.latex?\dpi{500}" + quote(code)

    async def request(self, code: str) -> bytes:
        async with self.session.get(self.url(code)) as response:
            if response.status == 200:
                result = await response.read()
                return result
