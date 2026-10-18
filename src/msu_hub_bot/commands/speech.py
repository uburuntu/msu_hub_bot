"""Voice, video-note and replied audio transcription through one speech provider."""

import asyncio
import io

from aiogram import html
from aiogram.types import Audio, Message, Video, VideoNote, Voice

from msu_hub_bot.execution.executor import ExecutorBusy, TPExecutor
from msu_hub_bot.media.ffmpeg import ffmpeg
from msu_hub_bot.media.limits import MAX_DOWNLOAD_BYTES
from msu_hub_bot.providers.speech import SpeechClient, SpeechError, SpeechErrorReason
from msu_hub_bot.settings import MissingIntegration
from msu_hub_bot.telegram.context import bot_for
from msu_hub_bot.telegram.files import DownloadTooLarge
from msu_hub_bot.telegram.media_jobs import DownloadUnavailable, run_downloaded
from msu_hub_bot.telegram.middlewares.settings import Settings
from msu_hub_bot.telegram.utils import send_super_reply
from msu_hub_bot.telemetry import Boundary, Outcome, Provider, Telemetry

# The default Qwen3-ASR model accepts up to 20 minutes per request; the cap also bounds upload size.
MAX_SPEECH_SECONDS = 20 * 60
PREPARATION_TIMEOUT = 120
RECOGNITION_CONCURRENCY = 3
SPEECH_REQUEST_TIMEOUT = 240
# Mono 16 kHz MP3 keeps a 20-minute clip under 5 MB in a format STT providers widely accept.
ENCODING = ["-vn", "-ac", "1", "-ar", "16000", "-t", str(MAX_SPEECH_SECONDS), "-codec:a", "libmp3lame", "-b:a", "32k"]


def prepare_audio(file: io.BytesIO) -> bytes | None:
    result = ffmpeg(file, out_suffix=".mp3", parameters=ENCODING, timeout=PREPARATION_TIMEOUT)
    if result is None:
        return None
    with result:
        return result.getvalue() or None


class Speech:
    def __init__(self, client: SpeechClient | None, executor: TPExecutor | None = None, *, telemetry: Telemetry | None = None) -> None:
        self.client = client
        self.executor = executor
        self.telemetry = telemetry or Telemetry()
        # Share provider capacity across messages, including queued uploads.
        self._recognition_slots = asyncio.Semaphore(RECOGNITION_CONCURRENCY)

    async def transcribe(self, audio: bytes) -> str:
        if self.client is None:
            raise MissingIntegration("openrouter_api_key")
        async with self._recognition_slots:
            with self.telemetry.operation(Boundary.PROVIDER, "speech.transcribe", provider=Provider.OPENROUTER) as observation:
                try:
                    transcript = await self.client.transcribe(audio)
                except SpeechError as error:
                    observation.set_outcome(Outcome.TIMEOUT if error.reason is SpeechErrorReason.TIMEOUT else Outcome.UNAVAILABLE)
                    raise
        return transcript.text.strip()

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
        if dest.duration > MAX_SPEECH_SECONDS:
            if explicit:
                return await message.reply("Распознаю записи до 20 минут — эта длиннее. Пришли фрагмент покороче.")
            return True

        if self.client is None:
            raise MissingIntegration("openrouter_api_key")

        if self.executor is None:
            raise RuntimeError("Speech executor is not configured")
        try:
            async with asyncio.timeout(SPEECH_REQUEST_TIMEOUT):
                audio, timeouted = await run_downloaded(self.executor, dest, prepare_audio, bot=bot_for(message))
                text = None if timeouted or not audio else await self.transcribe(audio)
        except TimeoutError:
            raise SpeechError(SpeechErrorReason.TIMEOUT) from None
        except DownloadUnavailable, DownloadTooLarge:
            return True
        except ExecutorBusy:
            if explicit:
                raise
            return True
        if not text:
            return True

        return await send_super_reply(target, html.quote(text))
