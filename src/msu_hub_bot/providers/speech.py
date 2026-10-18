"""OpenRouter speech-to-text for one prepared, size-bounded audio clip."""

import asyncio
import base64
import math
import re
from enum import StrEnum
from typing import Annotated

import aiohttp
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from msu_hub_bot.providers.exceptions import BadRequestError, ExternalServiceError
from msu_hub_bot.providers.http import read_limited

ENDPOINT = "https://openrouter.ai/api/v1/audio/transcriptions"
DEFAULT_MODEL = "qwen/qwen3-asr-1.7b"
MAX_AUDIO_BYTES = 16 * 1024 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024

type Seconds = Annotated[float, Field(ge=0, le=86_400, allow_inf_nan=False)]
type ReportedCost = Annotated[float, Field(ge=0, le=100, allow_inf_nan=False)]


class SpeechErrorReason(StrEnum):
    INVALID_REQUEST = "invalid_request"
    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"
    INVALID_RESPONSE = "invalid_response"
    CLOSED = "closed"


class SpeechError(ExternalServiceError):
    """A fixed failure category with no provider body, audio, headers or credentials."""

    def __init__(self, reason: SpeechErrorReason) -> None:
        super().__init__("Не удалось распознать речь. Попробуйте ещё раз позже.")
        self.reason = reason

    def __repr__(self) -> str:
        return f"SpeechError({self.reason.value})"


class Transcript(BaseModel):
    model_config = ConfigDict(frozen=True)

    text: str
    seconds: float | None = None
    cost: float | None = None


class _Usage(BaseModel):
    # Providers report different token/duration subsets; only typed known fields are kept.
    model_config = ConfigDict(strict=True, extra="ignore", hide_input_in_errors=True)

    seconds: Seconds | None = None
    cost: ReportedCost | None = None


class _Response(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", hide_input_in_errors=True)

    text: str = Field(max_length=MAX_RESPONSE_BYTES)
    usage: _Usage | None = None


class SpeechClient:
    """Reuse one lazy, owned session; composition must call close at shutdown.

    Routing is restricted to zero-data-retention endpoints that do not collect
    data, so a configured model without such an endpoint fails instead of
    silently sending chat audio elsewhere. Language is detected per recording
    because chats mix speakers and languages.
    """

    def __init__(self, api_key: str, *, model: str = DEFAULT_MODEL, timeout_seconds: float = 120.0) -> None:
        if not isinstance(api_key, str) or not api_key.strip() or "\r" in api_key or "\n" in api_key:
            raise SpeechError(SpeechErrorReason.UNAVAILABLE)
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._:-]*", model):
            raise ValueError("Speech model must be an OpenRouter model slug")
        if isinstance(timeout_seconds, bool) or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 300:
            raise ValueError("Speech timeout must be finite and between zero and 300 seconds")
        self._api_key = SecretStr(api_key)
        self.model = model
        self._timeout_seconds = timeout_seconds
        self._session: aiohttp.ClientSession | None = None
        self._closed = False

    def _get_session(self) -> aiohttp.ClientSession:
        if self._closed or (self._session is not None and self._session.closed):
            raise SpeechError(SpeechErrorReason.CLOSED)
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self._timeout_seconds, connect=min(10.0, self._timeout_seconds)),
                trust_env=False,
                cookie_jar=aiohttp.DummyCookieJar(),
                auto_decompress=False,
            )
        return self._session

    async def close(self) -> None:
        self._closed = True
        if self._session is not None:
            await self._session.close()

    async def transcribe(self, audio: bytes, audio_format: str = "mp3") -> Transcript:
        if not isinstance(audio, bytes) or not audio or len(audio) > MAX_AUDIO_BYTES:
            raise SpeechError(SpeechErrorReason.INVALID_REQUEST)
        if not re.fullmatch(r"[a-z0-9]{2,8}", audio_format):
            raise SpeechError(SpeechErrorReason.INVALID_REQUEST)

        payload: dict[str, object] = {
            "model": self.model,
            "input_audio": {"data": base64.b64encode(audio).decode("ascii"), "format": audio_format},
            "provider": {"zdr": True, "data_collection": "deny"},
        }
        try:
            async with asyncio.timeout(self._timeout_seconds):
                async with self._get_session().post(
                    ENDPOINT,
                    json=payload,
                    headers={
                        "Authorization": "Bearer " + self._api_key.get_secret_value(),
                        "Accept": "application/json",
                        "Accept-Encoding": "identity",
                    },
                    allow_redirects=False,
                    proxy=None,
                ) as response:
                    if response.status != 200:
                        raise SpeechError(SpeechErrorReason.UNAVAILABLE)
                    if response.headers.get("Content-Encoding", "identity").lower() not in {"", "identity"}:
                        raise SpeechError(SpeechErrorReason.INVALID_RESPONSE)
                    result = _Response.model_validate_json(await read_limited(response, MAX_RESPONSE_BYTES))
        except TimeoutError:
            raise SpeechError(SpeechErrorReason.TIMEOUT) from None
        except aiohttp.ClientError, OSError:
            raise SpeechError(SpeechErrorReason.UNAVAILABLE) from None
        except BadRequestError, ValidationError, ValueError, UnicodeError:
            raise SpeechError(SpeechErrorReason.INVALID_RESPONSE) from None

        usage = result.usage
        return Transcript(text=result.text, seconds=usage.seconds if usage else None, cost=usage.cost if usage else None)
