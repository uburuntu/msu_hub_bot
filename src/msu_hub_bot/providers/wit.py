from msu_hub_bot.settings import MissingIntegration

import asyncio
import io
from functools import cached_property
from itertools import cycle
from typing import List, Optional, cast
from typing_extensions import Buffer

import aiohttp
import pydub
from aiogram.types import Audio, Message, Video, VideoNote, Voice
from aiogram.utils.markdown import hitalic
from aiogram import html
from pydub.effects import normalize
from throttler import Throttler

from msu_hub_bot.telemetry import Boundary, Provider, Telemetry

from msu_hub_bot.execution.executor import TPExecutor
from msu_hub_bot.providers.exceptions import ExternalServiceError
from msu_hub_bot.telegram.middlewares.settings import Settings
from msu_hub_bot.telegram.runtime import gather_complete
from msu_hub_bot.telegram.utils import send_super_reply
from msu_hub_bot.telegram.context import bot_for
from msu_hub_bot.utils import megabytes, FakeBytesIO
from msu_hub_bot.media.ffmpeg import ffmpeg


class _AudioTooLarge(ValueError):
    pass


class _AudioBuffer(FakeBytesIO):
    def __init__(self, limit: int) -> None:
        super().__init__()
        self.limit = limit

    def write(self, data: Buffer) -> int:
        if self.tell() + memoryview(data).nbytes > self.limit:
            raise _AudioTooLarge()
        return super().write(data)


class WitAPIError(ExternalServiceError):
    def __init__(self, code: int, reason: str) -> None:
        super().__init__("Не удалось распознать речь. Попробуйте ещё раз позже.")
        self.code = code
        self.reason = reason

    def __repr__(self) -> str:
        return f"[{self.code}] {self.reason}"


class WitAPI:
    api_base = "https://api.wit.ai/"
    api_version = "20200513"

    def __init__(self, token: str, *, telemetry: Telemetry | None = None) -> None:
        self.token = token
        self.telemetry = telemetry or Telemetry()
        self.throttler = Throttler(rate_limit=60, period=60)

    @cached_property
    def session(self) -> aiohttp.ClientSession:
        headers = {"Authorization": f"Bearer {self.token}", "Accept": f"application/vnd.wit.{self.api_version}+json"}
        return aiohttp.ClientSession(headers=headers, timeout=aiohttp.ClientTimeout(total=30))

    async def close(self) -> None:
        session = self.__dict__.get("session")
        if session is not None:
            await session.close()

    async def _request(
        self, endpoint: str, method: str = "POST", headers: dict[str, str] | None = None, data: io.BytesIO | None = None, **params: str
    ) -> dict[str, object]:
        with self.telemetry.operation(Boundary.PROVIDER, "wit.recognize", provider=Provider.WIT):
            return await self._request_raw(endpoint, method, headers, data, **params)

    async def _request_raw(
        self, endpoint: str, method: str = "POST", headers: dict[str, str] | None = None, data: io.BytesIO | None = None, **params: str
    ) -> dict[str, object]:
        async with self.throttler:
            async with self.session.request(method, self.api_base + endpoint, headers=headers, data=data, params=params) as response:
                if response.status != 200:
                    text = await response.text()
                    if response.status == 400:
                        if "no-body" in text:
                            return {"text": ""}
                    raise WitAPIError(response.status, response.reason or "")
                try:
                    result = await response.json()
                except (aiohttp.ContentTypeError, ValueError):
                    raise WitAPIError(response.status, "Invalid response") from None
                if not isinstance(result, dict) or result.get("error") or ("text" in result and not isinstance(result["text"], str)):
                    raise WitAPIError(response.status, "Invalid response")
                return cast(dict[str, object], result)

    async def speech(self, audio: io.BytesIO, content_type: str = "audio/mpeg3") -> str:
        # Docs: https://wit.ai/docs/http/20200513/#post__speech_link
        result = await self._request("speech", headers={"Content-Type": content_type, "Cache-control": "no-cache"}, data=audio)
        text = result.get("text", "")
        return text if isinstance(text, str) else ""


class ManyWitAPI:
    def __init__(self, tokens: List[str], *, telemetry: Telemetry | None = None) -> None:
        self.instances = [WitAPI(t, telemetry=telemetry) for t in tokens]
        self.it = cycle(self.instances)

    async def close(self) -> None:
        await asyncio.gather(*[i.close() for i in self.instances])

    @property
    def instance(self) -> WitAPI:
        if not self.instances:
            raise MissingIntegration("wit_tokens")
        return next(self.it)


class Wit(ManyWitAPI):
    def __init__(self, tokens: List[str], executor: TPExecutor | None = None, *, telemetry: Telemetry | None = None) -> None:
        super().__init__(tokens, telemetry=telemetry)
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
            voice = audio[start:end].export(FakeBytesIO(), "mp3")
            result.append(voice)

        return result

    @staticmethod
    def to_mp3_chunks_2(file: io.BytesIO, duration: int, step: int = 19) -> List[io.BytesIO]:
        parameters = [
            "-f",
            "mp3",
            "-codec:a",
            "libmp3lame",
            "-vn",
        ]

        chunks = []
        for start in range(0, duration, step):
            file.seek(0)
            chunk = ffmpeg(file, out_suffix=".mp3", parameters=parameters + ["-ss", f"{start}", "-t", f"{step}"])
            if chunk is None:
                return []
            chunks.append(chunk)

        return chunks

    @staticmethod
    def to_raw_chunks(file: io.BytesIO, duration: int, max_size: int = 20_000, overlap: int = 100) -> List[io.BytesIO]:
        parameters = [
            "-f",
            "s16le",
            "-ar",
            "16000",
            "-ac",
            "1",
            "-af",
            "highpass=f=200,lowpass=f=2800",
            "-vn",
        ]

        step = max_size - 2 * overlap
        length = step + overlap
        duration = duration * 1000

        chunks = []
        for start in range(0, duration, step):
            file.seek(0)
            chunk = ffmpeg(file, out_suffix=".flac", parameters=parameters + ["-ss", f"{start}ms", "-t", f"{length}ms"])
            if chunk is None:
                return []
            chunks.append(chunk)

        return chunks

    async def stt(self, file: io.BytesIO, duration: int) -> Optional[str]:
        if self.executor is None:
            raise RuntimeError("Speech executor is not configured")
        chunks, timeouted = await self.executor.run(self.to_raw_chunks, file, duration)
        if timeouted or not chunks or any(chunk is None for chunk in chunks):
            return None

        texts: List[str] = await gather_complete(
            *[
                self.instance.speech(chunk, content_type="audio/raw;encoding=signed-integer;bits=16;rate=16000;endian=little")
                for chunk in chunks
            ]
        )

        if texts and texts[-1] == "":
            texts.pop()

        if not any(texts):
            return None

        text = " | ".join(html.quote(t) or hitalic("не распознано") for t in texts)
        return text

    async def process_stt_command(self, message: Message, settings: Settings) -> Message | bool | None:
        return await self.process_stt(message, settings, explicit=True)

    async def process_stt(self, message: Message, settings: Settings, *, explicit: bool = False) -> Message | bool | None:
        if not explicit and not settings.auto_speech_recognition:
            return True

        target = message
        dest: Voice | VideoNote | Audio | Video | None = target.voice or target.video_note

        if dest is None:
            if message.reply_to_message:
                target = message.reply_to_message
                dest = target.voice or target.video_note or target.audio or target.video

        limit = int(megabytes(20))
        if dest is None or (dest.file_size is not None and dest.file_size > limit):
            return True

        if not self.instances:
            raise MissingIntegration("wit_tokens")

        file = _AudioBuffer(limit)
        try:
            await bot_for(message).download(dest.file_id, destination=file)
        except _AudioTooLarge:
            return True
        file.seek(0)
        text = await self.stt(file, duration=int(dest.duration))
        if not text:
            return True

        return await send_super_reply(target, text)
