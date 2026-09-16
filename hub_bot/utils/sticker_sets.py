"""Keep upload references separate from the registered stickers used in replies."""

import asyncio
import hashlib
import io
import json
import logging
from dataclasses import dataclass

from aiogram.utils.exceptions import BadRequest, InvalidStickersSet, TelegramAPIError

logger = logging.getLogger(__name__)
LOOKUP_TIMEOUT = 5
LOOKUP_DELAYS = (0, 0.2, 0.5)
MAX_CONTENT_LOOKUPS = 3
MAX_STICKER_BYTES = 512 * 1024


@dataclass(frozen=True)
class UploadedSticker:
    file_id: str
    format: str
    emojis: tuple[str, ...]
    file_unique_id: str | None = None
    sha256: str | None = None
    size: int | None = None

    def input_sticker(self):
        return {"sticker": self.file_id, "format": self.format, "emoji_list": list(self.emojis)}

    def metadata(self):
        return {"file_unique_id": self.file_unique_id, "sha256": self.sha256, "size": self.size}

    @classmethod
    def from_pending(cls, data):
        # Preserve title prompts created before upload metadata was stored.
        sticker = data["mixed_sticker"]
        metadata = data.get("sticker_upload", {})
        return cls(sticker["sticker"], sticker["format"], tuple(sticker["emoji_list"]), **metadata)


class StickerSetClient:
    def __init__(self, bot):
        self.bot = bot

    async def upload(self, user_id, payload, kind, emojis):
        suffix = {"static": "webp", "animated": "tgs", "video": "webm"}[kind]
        uploaded = await self.bot.request(
            "uploadStickerFile",
            {"user_id": user_id, "sticker_format": kind},
            files={"sticker": (f"sticker.{suffix}", io.BytesIO(payload))},
        )
        return UploadedSticker(
            uploaded["file_id"], kind, tuple(emojis), uploaded["file_unique_id"], hashlib.sha256(payload).hexdigest(), len(payload)
        )

    async def _add(self, name, user_id, sticker):
        await self.bot.request(
            "addStickerToSet",
            {"user_id": user_id, "name": name, "sticker": json.dumps(sticker.input_sticker(), ensure_ascii=False)},
        )

    async def save(self, name, user_id, sticker, title=None):
        """Return False if a title is needed; True after a confirmed save.

        An uncertain network result is never retried as a mutation.
        """
        try:
            await self.bot.get_sticker_set(name)
        except InvalidStickersSet:
            if title is None:
                return False
            try:
                await self.bot.request(
                    "createNewStickerSet",
                    {
                        "user_id": user_id,
                        "name": name,
                        "title": title,
                        "stickers": json.dumps([sticker.input_sticker()], ensure_ascii=False),
                        "sticker_type": "regular",
                    },
                )
            except BadRequest as creation_error:
                # A concurrent admin may have created this exact chat pack.
                # Check its existence instead of parsing Telegram's error wording.
                try:
                    await self.bot.get_sticker_set(name)
                except InvalidStickersSet:
                    raise creation_error
                await self._add(name, user_id, sticker)
        else:
            await self._add(name, user_id, sticker)
        return True

    @staticmethod
    def _same_format(sticker, kind):
        actual = "animated" if sticker.is_animated else "video" if sticker.is_video else "static"
        return actual == kind and sticker.type == "regular"

    async def resolve(self, name, uploaded):
        """Return a verified pack sticker ID, or None; never fall back to the upload.

        file_unique_id handles reordering and duplicates. If Telegram assigns a
        new identity, an exact byte comparison can still identify the media.
        Missing/ambiguous results fall back to a pack link at the caller.
        """
        checked = set()
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
                            data = await self.bot.download_file_by_id(sticker.file_id, io.BytesIO())
                            payload = data.getvalue()
                            if len(payload) == uploaded.size and hashlib.sha256(payload).hexdigest() == uploaded.sha256:
                                return sticker.file_id
        except (TimeoutError, TelegramAPIError):
            pass
        logger.warning("Saved sticker preview could not be resolved")
        return None
