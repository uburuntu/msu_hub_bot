"""Explicit ownership of Telegram downloads and multipart uploads."""

import io
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    Animation,
    Audio,
    BufferedInputFile,
    Document,
    FSInputFile,
    InputFile,
    PhotoSize,
    Sticker,
    Video,
    VideoNote,
    Voice,
)

from common.tg.context import bot_for

DownloadableMedia = Animation | Audio | Document | PhotoSize | Sticker | Video | VideoNote | Voice


def input_file(value: bytes | io.BytesIO | Path | str, filename: str | None = None) -> InputFile:
    """Snapshot in-memory content; leave local-file lifetime with the caller."""
    if isinstance(value, io.BytesIO):
        return BufferedInputFile(value.getvalue(), filename or "file")
    if isinstance(value, bytes):
        return BufferedInputFile(value, filename or "file")
    return FSInputFile(value, filename=filename)


async def download(media: DownloadableMedia | None, bot: Bot | None = None) -> io.BytesIO | None:
    if media is None:
        return None
    return await download_by_file_id(media.file_id, bot or bot_for(media))


async def download_by_file_id(file_id: str, bot: Bot) -> io.BytesIO | None:
    destination = io.BytesIO()
    try:
        await bot.download(file_id, destination=destination)
    except TelegramBadRequest:
        destination.close()
        return None
    except BaseException:
        destination.close()
        raise
    destination.seek(0)
    return destination


async def download_text(file_id: str, bot: Bot) -> str | None:
    stream = await download_by_file_id(file_id, bot)
    if stream is None:
        return None
    with stream:
        return stream.read().decode("utf-8")
