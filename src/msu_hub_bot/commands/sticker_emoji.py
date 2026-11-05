"""Optional Jev emoji suggestions for sticker artwork without its own emoji."""

import asyncio

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import PhotoSize, Sticker

from msu_hub_bot.execution.executor import TPExecutor
from msu_hub_bot.media.sticker_media import StickerMediaError, emoji_preview
from msu_hub_bot.providers.jev import STICKER_EMOJI, JevClient, JevError, JevErrorReason
from msu_hub_bot.telegram.files import DownloadableMedia, DownloadTooLarge, download
from msu_hub_bot.telemetry import Boundary, Outcome, Provider, Telemetry

TIMEOUT = 10
MAX_PREVIEW_BYTES = 5 * 1024 * 1024
MAX_EMOJIS = 3
TOP_PROBABILITY = 0.3
EXTRA_PROBABILITY = 0.2


def select_emojis(probabilities: dict[str, float]) -> tuple[str, ...]:
    """Take up to three tags: the top one at 0.3 or more, the others at 0.2 or more."""
    ranked = sorted(probabilities.items(), key=lambda item: item[1], reverse=True)
    if not ranked or ranked[0][1] < TOP_PROBABILITY:
        return ()
    chosen = [STICKER_EMOJI[ranked[0][0]][0]]
    for tag, probability in ranked[1:MAX_EMOJIS]:
        if probability < EXTRA_PROBABILITY:
            break
        chosen.append(STICKER_EMOJI[tag][0])
    return tuple(dict.fromkeys(chosen))


def preview_source(source: DownloadableMedia) -> PhotoSize | None:
    """Telegram's own still preview: the photo itself or a media thumbnail."""
    if isinstance(source, PhotoSize):
        return source
    thumbnail = getattr(source, "thumbnail", None)
    return thumbnail if isinstance(thumbnail, PhotoSize) else None


class StickerEmojiPicker:
    """Send only a downscaled still of the artwork; no text, names or chat identities."""

    def __init__(self, client: JevClient, *, telemetry: Telemetry) -> None:
        self.client = client
        self.telemetry = telemetry

    async def pick(self, source: DownloadableMedia, bot: Bot, cpu_executor: TPExecutor) -> tuple[str, ...]:
        if isinstance(source, Sticker) and source.type == "custom_emoji":
            return ()
        if (preview := preview_source(source)) is None:
            return ()
        with self.telemetry.operation(Boundary.PROVIDER, "jev.sticker_emoji", provider=Provider.JEV) as observation:
            try:
                async with asyncio.timeout(TIMEOUT):
                    file = await download(preview, bot, max_bytes=MAX_PREVIEW_BYTES)
                    if file is None:
                        observation.set_outcome(Outcome.UNAVAILABLE)
                        return ()
                    with file:
                        image, timeouted = await cpu_executor.run(emoji_preview, file.getvalue(), timeout=TIMEOUT)
                    if timeouted:
                        observation.set_outcome(Outcome.TIMEOUT)
                        return ()
                    probabilities = await self.client.sticker_emojis(image)
            except TimeoutError:
                observation.set_outcome(Outcome.TIMEOUT)
                return ()
            except TelegramAPIError:
                observation.set_outcome(Outcome.UNAVAILABLE)
                return ()
            except JevError as error:
                observation.set_outcome(Outcome.TIMEOUT if error.reason is JevErrorReason.TIMEOUT else Outcome.UNAVAILABLE)
                return ()
            except DownloadTooLarge, StickerMediaError:
                observation.set_outcome(Outcome.REJECTED)
                return ()
            return select_emojis(probabilities)
