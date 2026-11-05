"""Optional Jev language detection for commands that otherwise assume one."""

import asyncio

from msu_hub_bot.execution.executor import TPExecutor
from msu_hub_bot.media.sticker_media import StickerMediaError, jpeg_still
from msu_hub_bot.providers.jev import MAX_REQUEST_LENGTH, JevClient, JevError, JevErrorReason
from msu_hub_bot.telemetry import Boundary, Outcome, Provider, Telemetry

TIMEOUT = 10
MIN_PROBABILITY = 0.6


class LanguageDetector:
    """Return an ISO 639-1 code only for a confident answer; callers keep their defaults otherwise.

    Text detection sends at most the first 1500 characters. Image detection sends one
    downscaled JPEG still. No names, chat identities or other message fields are sent.
    """

    def __init__(self, client: JevClient, *, telemetry: Telemetry) -> None:
        self.client = client
        self.telemetry = telemetry

    async def detect_text(self, text: str) -> str | None:
        sample = text.strip()[:MAX_REQUEST_LENGTH]
        if not sample:
            return None
        return await self._detect(text=sample)

    async def detect_image(self, data: bytes, cpu_executor: TPExecutor) -> str | None:
        try:
            image, timeouted = await cpu_executor.run(jpeg_still, data, timeout=TIMEOUT)
        except StickerMediaError:
            return None
        return None if timeouted else await self._detect(image=image)

    async def _detect(self, *, text: str | None = None, image: bytes | None = None) -> str | None:
        with self.telemetry.operation(Boundary.PROVIDER, "jev.language", provider=Provider.JEV) as observation:
            try:
                async with asyncio.timeout(TIMEOUT):
                    probabilities = await self.client.language(text=text, image=image)
            except TimeoutError:
                observation.set_outcome(Outcome.TIMEOUT)
                return None
            except JevError as error:
                observation.set_outcome(Outcome.TIMEOUT if error.reason is JevErrorReason.TIMEOUT else Outcome.UNAVAILABLE)
                return None
            code, probability = max(probabilities.items(), key=lambda item: item[1])
            return code if code != "other" and probability >= MIN_PROBABILITY else None
