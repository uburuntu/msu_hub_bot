from msu_hub_bot.settings import MissingIntegration

import asyncio
import io
import time
from contextlib import ExitStack
from functools import cached_property
from itertools import cycle
from typing import List, Optional, cast

import aiohttp
from aiogram.types import Audio, Message, Video, VideoNote, Voice
from aiogram.utils.markdown import hitalic
from aiogram import html
from throttler import Throttler

from msu_hub_bot.telemetry import Boundary, Provider, Telemetry

from msu_hub_bot.execution.executor import ExecutorBusy, TPExecutor
from msu_hub_bot.providers.exceptions import ExternalServiceError
from msu_hub_bot.telegram.middlewares.settings import Settings
from msu_hub_bot.telegram.runtime import gather_complete
from msu_hub_bot.telegram.utils import send_super_reply
from msu_hub_bot.telegram.context import bot_for
from msu_hub_bot.telegram.files import DownloadTooLarge
from msu_hub_bot.telegram.media_jobs import DownloadUnavailable, run_downloaded
from msu_hub_bot.media.limits import MAX_DOWNLOAD_BYTES
from msu_hub_bot.media.ffmpeg import ffmpeg

CHUNK_PREPARATION_TIMEOUT = 120
MAX_PCM_BYTES = 64 * 1024 * 1024
RECOGNITION_CONCURRENCY = 3
RECOGNITION_TIMEOUT = 180
SPEECH_REQUEST_TIMEOUT = 240


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
                except aiohttp.ContentTypeError, ValueError:
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
        # Share capacity across messages and tokens, including provider rate-limit waits.
        self._recognition_slots = asyncio.Semaphore(RECOGNITION_CONCURRENCY)

    @staticmethod
    def to_raw_chunks(file: io.BytesIO, duration: int, max_size: int = 20_000, overlap: int = 100) -> List[io.BytesIO]:
        if any(not isinstance(value, int) or isinstance(value, bool) for value in (duration, max_size, overlap)):
            return []
        if duration <= 0 or overlap < 0 or max_size <= 2 * overlap:
            return []
        deadline = time.monotonic() + CHUNK_PREPARATION_TIMEOUT
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

        chunks: list[io.BytesIO] = []
        total_bytes = 0
        complete = False
        try:
            for start in range(0, duration, step):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return []
                file.seek(0)
                chunk = ffmpeg(
                    file,
                    out_suffix=".flac",
                    parameters=parameters + ["-ss", f"{start}ms", "-t", f"{length}ms"],
                    timeout=remaining,
                )
                if chunk is None:
                    return []
                chunks.append(chunk)
                with chunk.getbuffer() as view:
                    size = view.nbytes
                total_bytes += size
                if not size or total_bytes > MAX_PCM_BYTES or time.monotonic() >= deadline:
                    return []
            complete = True
            return chunks
        finally:
            if not complete:
                for chunk in chunks:
                    chunk.close()

    @staticmethod
    def _prepare_audio(file: io.BytesIO, duration: int) -> tuple[bytes, ...]:
        chunks = Wit.to_raw_chunks(file, duration)
        try:
            return tuple(chunk.getvalue() for chunk in chunks)
        finally:
            for chunk in chunks:
                chunk.close()

    async def stt(self, file: io.BytesIO, duration: int) -> Optional[str]:
        if self.executor is None:
            raise RuntimeError("Speech executor is not configured")
        try:
            async with asyncio.timeout(SPEECH_REQUEST_TIMEOUT):
                chunks, timeouted = await self.executor.run(self._prepare_audio, file, duration)
                if timeouted:
                    return None
                return await self._recognize_chunks(chunks)
        except TimeoutError:
            raise WitAPIError(408, "Speech deadline exceeded") from None

    async def _recognize_chunks(self, chunks: tuple[bytes, ...] | None) -> Optional[str]:
        if not chunks or any(not chunk for chunk in chunks):
            return None

        async def recognize(chunk: io.BytesIO) -> str:
            async with self._recognition_slots:
                return await self.instance.speech(chunk, content_type="audio/raw;encoding=signed-integer;bits=16;rate=16000;endian=little")

        try:
            async with asyncio.timeout(RECOGNITION_TIMEOUT):
                with ExitStack() as buffers:
                    audio = [buffers.enter_context(io.BytesIO(chunk)) for chunk in chunks]
                    texts = await gather_complete(*(recognize(chunk) for chunk in audio))
        except TimeoutError:
            raise WitAPIError(408, "Recognition deadline exceeded") from None

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

        if dest is None or (dest.file_size is not None and dest.file_size > MAX_DOWNLOAD_BYTES):
            return True

        if not self.instances:
            raise MissingIntegration("wit_tokens")

        if self.executor is None:
            raise RuntimeError("Speech executor is not configured")
        try:
            async with asyncio.timeout(SPEECH_REQUEST_TIMEOUT):
                chunks, timeouted = await run_downloaded(self.executor, dest, self._prepare_audio, int(dest.duration), bot=bot_for(message))
                text = None if timeouted else await self._recognize_chunks(chunks)
        except TimeoutError:
            raise WitAPIError(408, "Speech deadline exceeded") from None
        except DownloadUnavailable, DownloadTooLarge:
            return True
        except ExecutorBusy:
            if explicit:
                raise
            return True
        if not text:
            return True

        return await send_super_reply(target, text)
