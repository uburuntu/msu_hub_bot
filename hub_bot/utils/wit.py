from msu_hub_bot.settings import MissingIntegration

import asyncio
import io
from functools import cached_property
from itertools import cycle
from typing import List, Optional

import aiohttp
import pydub
from aiogram.types import Message
from aiogram.utils.markdown import hitalic, quote_html
from pydub.effects import normalize
from throttler import Throttler

from common.executor import PPExecutor
from common.tg.middlewares.settings import Settings
from common.tg.utils import send_super_reply
from common.utils import megabytes, FakeBytesIO
from utils.ffmpeg import ffmpeg


class _AudioTooLarge(ValueError):
    pass


class _AudioBuffer(FakeBytesIO):
    def __init__(self, limit: int):
        super().__init__()
        self.limit = limit

    def write(self, data):
        if self.tell() + len(data) > self.limit:
            raise _AudioTooLarge()
        return super().write(data)


class WitAPIError(Exception):
    def __init__(self, code: int, reason: str):
        self.code = code
        self.reason = reason

    def __repr__(self):
        return f'[{self.code}] {self.reason}'


class WitAPI:
    api_base = 'https://api.wit.ai/'
    api_version = '20200513'

    def __init__(self, token: str):
        self.token = token
        self.throttler = Throttler(rate_limit=60, period=60)

    @cached_property
    def session(self) -> aiohttp.ClientSession:
        headers = {
            'Authorization': f'Bearer {self.token}',
            'Accept': f'application/vnd.wit.{self.api_version}+json'
        }
        return aiohttp.ClientSession(headers=headers)

    async def close(self):
        session = self.__dict__.get('session')
        if session is not None:
            await session.close()

    async def _request(self, endpoint: str, method: str = 'POST', headers: dict = None, data=None, **params) -> dict:
        async with self.throttler:
            async with self.session.request(method, self.api_base + endpoint, headers=headers, data=data, params=params) as response:
                if response.status != 200:
                    text = await response.text()
                    if response.status == 400:
                        if 'no-body' in text:
                            return {'text': ''}
                    raise WitAPIError(response.status, response.reason + ' ' + text)
                result = await response.json()
                return result

    async def speech(self, audio: io.BytesIO, content_type: str = 'audio/mpeg3') -> str:
        # Docs: https://wit.ai/docs/http/20200513/#post__speech_link
        result = await self._request('speech', headers={'Content-Type': content_type, 'Cache-control': 'no-cache'}, data=audio)
        return result.get('text', '')


class ManyWitAPI:
    def __init__(self, tokens: List[str]):
        self.instances = [WitAPI(t) for t in tokens]
        self.it = cycle(self.instances)

    async def close(self):
        await asyncio.gather(*[i.close() for i in self.instances])

    @property
    def instance(self) -> WitAPI:
        if not self.instances:
            raise MissingIntegration('wit_tokens')
        return next(self.it)


class Wit(ManyWitAPI):
    def __init__(self, tokens: List[str], executor: PPExecutor = None):
        super().__init__(tokens)
        self.executor = executor

    @staticmethod
    def to_mp3_chunks(file: io.BytesIO, max_size: int = 20_000, overlap: int = 400, threshold: int = 100) -> List[io.BytesIO]:
        audio = pydub.AudioSegment.from_file(file)
        audio = normalize(audio, headroom=0.5)

        step = max_size - 2 * overlap - threshold
        duration = int(audio.duration_seconds * 1000)

        result = []
        for offset in range(0, duration, step):
            start, end = max(offset - overlap, 0), min(offset + step + overlap, duration)
            voice = audio[start:end].export(FakeBytesIO(), 'mp3')
            result.append(voice)

        return result

    @staticmethod
    def to_mp3_chunks_2(file: io.BytesIO, duration: int, step: int = 19) -> List[io.BytesIO]:
        parameters = [
            '-f', 'mp3',
            '-codec:a', 'libmp3lame',
            '-vn',
        ]

        chunks = []
        for start in range(0, duration, step):
            file.seek(0)
            chunk = ffmpeg(file, out_suffix='.mp3', parameters=parameters + ['-ss', f'{start}', '-t', f'{step}'])
            chunks.append(chunk)

        return chunks

    @staticmethod
    def to_raw_chunks(file: io.BytesIO, duration: int, max_size: int = 20_000, overlap: int = 100) -> List[io.BytesIO]:
        parameters = [
            '-f', 's16le',
            '-ar', '16000',
            '-ac', '1',
            '-af', 'highpass=f=200,lowpass=f=2800',
            '-vn',
        ]

        step = max_size - 2 * overlap
        length = step + overlap
        duration = duration * 1000

        chunks = []
        for start in range(0, duration, step):
            file.seek(0)
            chunk = ffmpeg(file, out_suffix='.flac', parameters=parameters + ['-ss', f'{start}ms', '-t', f'{length}ms'])
            if chunk is None:
                return []
            chunks.append(chunk)

        return chunks

    async def stt(self, file: io.BytesIO, duration: int) -> Optional[str]:
        chunks, timeouted = await self.executor.run(self.to_raw_chunks, file, duration)
        if timeouted or not chunks or any(chunk is None for chunk in chunks):
            return None

        texts: List[str] = await asyncio.gather(*[self.instance.speech(chunk, content_type='audio/raw;encoding=signed-integer;bits=16;rate=16000;endian=little')
                                                  for chunk in chunks])

        if texts and texts[-1] == '':
            texts.pop()

        if not any(texts):
            return None

        text = ' | '.join(quote_html(t) or hitalic('не распознано') for t in texts)
        return text

    async def process_stt_command(self, message: Message, settings: Settings):
        return await self.process_stt(message, settings, explicit=True)

    async def process_stt(self, message: Message, settings: Settings, *, explicit: bool = False):
        if not explicit and not settings.auto_speech_recognition:
            return True

        target = message
        dest = target.voice or target.video_note

        if dest is None:
            if target := message.reply_to_message:
                dest = target.voice or target.video_note or target.audio or target.video

        limit = int(megabytes(20))
        if dest is None or (dest.file_size is not None and dest.file_size > limit):
            return True

        if not self.instances:
            raise MissingIntegration('wit_tokens')

        file = _AudioBuffer(limit)
        try:
            await dest.download(destination_file=file)
        except _AudioTooLarge:
            return True
        file.seek(0)
        text = await self.stt(file, duration=dest.duration)
        if not text:
            return True

        return await send_super_reply(target, text)
