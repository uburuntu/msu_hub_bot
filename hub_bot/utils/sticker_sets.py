"""Keep upload references separate from the registered stickers used in replies."""

import asyncio
import hashlib
import io
import logging
from dataclasses import dataclass
from collections.abc import Mapping, Sequence
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramAPIError
from aiogram.types import InputSticker, Sticker
from pydantic import BaseModel

from common.tg.files import input_file

logger = logging.getLogger(__name__)
LOOKUP_TIMEOUT = 5
LOOKUP_DELAYS = (0, 0.2, 0.5)
MAX_CONTENT_LOOKUPS = 3
MAX_STICKER_BYTES = 512 * 1024


def sticker_error(error: TelegramBadRequest, code: str) -> bool:
    """Bot API error codes distinguish the v2 exceptions merged into BadRequest."""
    return code.casefold() in error.message.casefold()


class UploadMetadata(BaseModel):
    file_unique_id: str | None = None
    sha256: str | None = None
    size: int | None = None


@dataclass(frozen=True)
class UploadedSticker:
    file_id: str
    format: str
    emojis: tuple[str, ...]
    file_unique_id: str | None = None
    sha256: str | None = None
    size: int | None = None

    def input_sticker(self) -> InputSticker:
        return InputSticker(sticker=self.file_id, format=self.format, emoji_list=list(self.emojis))

    def metadata(self) -> dict[str, str | int | None]:
        return {"file_unique_id": self.file_unique_id, "sha256": self.sha256, "size": self.size}

    @classmethod
    def from_pending(cls, data: Mapping[str, Any]) -> "UploadedSticker":
        sticker = InputSticker.model_validate(data["mixed_sticker"])
        metadata = UploadMetadata.model_validate(data.get("sticker_upload", {}))
        if not isinstance(sticker.sticker, str):
            raise ValueError("Pending stickers must reference an uploaded file")
        return cls(sticker.sticker, sticker.format, tuple(sticker.emoji_list), metadata.file_unique_id, metadata.sha256, metadata.size)


class StickerSetClient:
    def __init__(self, bot: Bot) -> None:
        self.bot = bot

    async def upload(self, user_id: int, payload: bytes, kind: str, emojis: Sequence[str]) -> UploadedSticker:
        suffix = {"static": "webp", "animated": "tgs", "video": "webm"}[kind]
        uploaded = await self.bot.upload_sticker_file(
            user_id=user_id,
            sticker=input_file(payload, f"sticker.{suffix}"),
            sticker_format=kind,
        )
        return UploadedSticker(
            uploaded.file_id, kind, tuple(emojis), uploaded.file_unique_id, hashlib.sha256(payload).hexdigest(), len(payload)
        )

    async def _add(self, name: str, user_id: int, sticker: UploadedSticker) -> None:
        await self.bot.add_sticker_to_set(user_id=user_id, name=name, sticker=sticker.input_sticker())

    async def save(self, name: str, user_id: int, sticker: UploadedSticker, title: str | None = None) -> bool:
        """Return False if a title is needed; True after a confirmed save.

        An uncertain network result is never retried as a mutation.
        """
        try:
            await self.bot.get_sticker_set(name)
        except TelegramBadRequest as lookup_error:
            if not sticker_error(lookup_error, "STICKERSET_INVALID"):
                raise
            if title is None:
                return False
            try:
                await self.bot.create_new_sticker_set(
                    user_id=user_id,
                    name=name,
                    title=title,
                    stickers=[sticker.input_sticker()],
                    sticker_type="regular",
                )
            except TelegramBadRequest as creation_error:
                # A concurrent admin may have created this exact chat pack.
                try:
                    await self.bot.get_sticker_set(name)
                except TelegramBadRequest as retry_error:
                    if not sticker_error(retry_error, "STICKERSET_INVALID"):
                        raise
                    raise creation_error
                await self._add(name, user_id, sticker)
        else:
            await self._add(name, user_id, sticker)
        return True

    @staticmethod
    def _same_format(sticker: Sticker, kind: str) -> bool:
        actual = "animated" if sticker.is_animated else "video" if sticker.is_video else "static"
        return actual == kind and sticker.type == "regular"

    async def resolve(self, name: str, uploaded: UploadedSticker) -> str | None:
        """Return a verified pack sticker ID, or None; never fall back to the upload.

        file_unique_id handles reordering and duplicates. If Telegram assigns a
        new identity, an exact byte comparison can still identify the media.
        Missing/ambiguous results fall back to a pack link at the caller.
        """
        checked: set[str] = set()
        try:
            async with asyncio.timeout(LOOKUP_TIMEOUT):
                unique_id = uploaded.file_unique_id
                if unique_id is None:
                    unique_id = (await self.bot.get_file(uploaded.file_id)).file_unique_id
                for delay in LOOKUP_DELAYS:
                    if delay:
                        await asyncio.sleep(delay)
                    pack = await self.bot.get_sticker_set(name)
                    candidates = [s for s in pack.stickers if self._same_format(s, uploaded.format)]
                    for sticker in candidates:
                        if unique_id and sticker.file_unique_id == unique_id:
                            return sticker.file_id
                    if uploaded.sha256 and uploaded.size is not None and 0 < uploaded.size <= MAX_STICKER_BYTES:
                        # Order only prioritizes downloads; it never determines the reply.
                        for sticker in reversed(candidates):
                            if len(checked) >= MAX_CONTENT_LOOKUPS:
                                break
                            if sticker.file_size != uploaded.size or sticker.file_unique_id in checked:
                                continue
                            checked.add(sticker.file_unique_id)
                            data = io.BytesIO()
                            await self.bot.download(sticker.file_id, destination=data)
                            payload = data.getvalue()
                            if len(payload) == uploaded.size and hashlib.sha256(payload).hexdigest() == uploaded.sha256:
                                return sticker.file_id
        except (TimeoutError, TelegramAPIError):
            pass
        logger.warning("Saved sticker preview could not be resolved")
        return None
