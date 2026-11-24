"""Keep upload references separate from the registered stickers used in replies."""

import asyncio
import hashlib
import logging
import re
from dataclasses import dataclass
from collections.abc import Mapping, Sequence
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramAPIError
from aiogram.types import InputSticker, Sticker, StickerSet
from pydantic import BaseModel

from msu_hub_bot.telegram.files import DownloadTooLarge, download_by_file_id, input_file

logger = logging.getLogger(__name__)
LOOKUP_TIMEOUT = 5
LOOKUP_DELAYS = (0, 0.2, 0.5)
MAX_CONTENT_LOOKUPS = 3
MAX_STICKER_BYTES = 512 * 1024
# Telegram's limit for regular sets; custom emoji sets allow 200.
MAX_PACK_STICKERS = 120
# Bounds the reads per save: every full pack is read before the one with room.
MAX_SERIES_PACKS = 10
MAX_TITLE_LENGTH = 64


class PackTitleRequired(Exception):
    """The first pack of a series does not exist yet and needs a title."""


class PackSeriesFull(Exception):
    """Every pack of the series holds the Telegram maximum."""


def series_pack_name(name: str, index: int) -> str:
    """Pack 1 keeps its name; later packs insert their number before the bot suffix."""
    if index == 1:
        return name
    head, separator, bot_suffix = name.rpartition("_by_")
    if not separator:
        return f"{name}_{index}"
    return f"{head}_{index}{separator}{bot_suffix}"


def series_index(name: str, first: str) -> int | None:
    """Return the position of name in the series that starts with first, if any."""
    if name == first:
        return 1
    head, separator, bot_suffix = first.rpartition("_by_")
    if separator:
        pattern = rf"{re.escape(head)}_([1-9][0-9]*){re.escape(separator + bot_suffix)}"
    else:
        pattern = rf"{re.escape(first)}_([1-9][0-9]*)"
    if (match := re.fullmatch(pattern, name, flags=re.ASCII)) and (index := int(match[1])) > 1:
        return index
    return None


def series_title(title: str, index: int) -> str:
    """Later packs repeat the first pack's title with their number."""
    suffix = f" ({index})"
    return title[: MAX_TITLE_LENGTH - len(suffix)].rstrip() + suffix


def sticker_error(error: TelegramBadRequest, code: str) -> bool:
    """Bot API error codes distinguish the v2 exceptions merged into BadRequest."""
    return code.casefold() in error.message.casefold()


class UploadMetadata(BaseModel):
    file_unique_id: str | None = None
    sha256: str | None = None
    size: int | None = None
    emojis_explicit: bool = False


@dataclass(frozen=True)
class UploadedSticker:
    file_id: str
    format: str
    emojis: tuple[str, ...]
    file_unique_id: str | None = None
    sha256: str | None = None
    size: int | None = None
    keywords: tuple[str, ...] | None = None
    emojis_explicit: bool = False

    def input_sticker(self) -> InputSticker:
        return InputSticker(
            sticker=self.file_id,
            format=self.format,
            emoji_list=list(self.emojis),
            keywords=list(self.keywords) if self.keywords is not None else None,
        )

    def metadata(self) -> dict[str, str | int | bool | None]:
        return {
            "file_unique_id": self.file_unique_id,
            "sha256": self.sha256,
            "size": self.size,
            "emojis_explicit": self.emojis_explicit,
        }

    @classmethod
    def from_pending(cls, data: Mapping[str, Any]) -> "UploadedSticker":
        sticker = InputSticker.model_validate(data["mixed_sticker"])
        metadata = UploadMetadata.model_validate(data.get("sticker_upload", {}))
        if not isinstance(sticker.sticker, str):
            raise ValueError("Pending stickers must reference an uploaded file")
        return cls(
            sticker.sticker,
            sticker.format,
            tuple(sticker.emoji_list),
            metadata.file_unique_id,
            metadata.sha256,
            metadata.size,
            tuple(sticker.keywords) if sticker.keywords is not None else None,
            metadata.emojis_explicit,
        )


@dataclass(frozen=True)
class SavedSticker:
    """The pack holding a saved sticker; created when this save started the pack."""

    name: str
    index: int = 1
    created: bool = False

    @property
    def started_next_pack(self) -> bool:
        return self.created and self.index > 1


class StickerSetClient:
    def __init__(self, bot: Bot) -> None:
        self.bot = bot
        # Pack identities immediately before this client's own addition or creation.
        self._before_addition: dict[str, frozenset[str]] = {}

    async def upload(
        self,
        user_id: int,
        payload: bytes,
        kind: str,
        emojis: Sequence[str],
        *,
        keywords: tuple[str, ...] | None = None,
        emojis_explicit: bool = False,
    ) -> UploadedSticker:
        suffix = {"static": "webp", "animated": "tgs", "video": "webm"}[kind]
        uploaded = await self.bot.upload_sticker_file(
            user_id=user_id,
            sticker=input_file(payload, f"sticker.{suffix}"),
            sticker_format=kind,
        )
        return UploadedSticker(
            uploaded.file_id,
            kind,
            tuple(emojis),
            uploaded.file_unique_id,
            hashlib.sha256(payload).hexdigest(),
            len(payload),
            keywords,
            emojis_explicit,
        )

    async def update_metadata(self, file_id: str, sticker: UploadedSticker) -> bool:
        """Apply explicit metadata to the resolved target-pack sticker after save.

        Exact duplicate additions are a no-op in Telegram, including concurrent
        additions after the initial lookup. A failed metadata update never turns
        a confirmed save into a mutation retry.
        """
        try:
            async with asyncio.timeout(LOOKUP_TIMEOUT):
                if sticker.emojis_explicit:
                    await self.bot.set_sticker_emoji_list(sticker=file_id, emoji_list=list(sticker.emojis))
                if sticker.keywords is not None:
                    await self.bot.set_sticker_keywords(sticker=file_id, keywords=list(sticker.keywords))
        except TimeoutError, TelegramAPIError:
            logger.warning("Saved sticker metadata could not be updated")
            return False
        return True

    async def _add(self, name: str, user_id: int, sticker: UploadedSticker, current: Sequence[Sticker]) -> None:
        self._before_addition[name] = frozenset(item.file_unique_id for item in current)
        await self.bot.add_sticker_to_set(user_id=user_id, name=name, sticker=sticker.input_sticker())

    @classmethod
    def _contains_reference(cls, stickers: Sequence[Sticker], uploaded: UploadedSticker) -> bool:
        return any(
            cls._same_format(sticker, uploaded.format)
            and (sticker.file_id == uploaded.file_id or bool(uploaded.file_unique_id and sticker.file_unique_id == uploaded.file_unique_id))
            for sticker in stickers
        )

    async def save(self, name: str, user_id: int, sticker: UploadedSticker, title: str | None = None) -> SavedSticker:
        """Register the sticker in the first pack of the series that has room.

        name is the series' first pack. Without it, a title is required
        (PackTitleRequired). When every existing pack is full, the next one is
        created with the first pack's title and number, up to MAX_SERIES_PACKS.
        Known references in the packs read on the way are not added again. A
        definite rejection for a full pack moves on to the next; an uncertain
        network result is never retried as a mutation.
        """
        first: StickerSet | None = None
        for index in range(1, MAX_SERIES_PACKS + 1):
            pack_name = series_pack_name(name, index)
            try:
                pack = await self.bot.get_sticker_set(pack_name)
            except TelegramBadRequest as lookup_error:
                if not sticker_error(lookup_error, "STICKERSET_INVALID"):
                    raise
                if first is not None:
                    return await self._create(pack_name, index, user_id, sticker, series_title(first.title, index))
                if title is None:
                    raise PackTitleRequired from None
                return await self._create(pack_name, index, user_id, sticker, title)
            if first is None:
                first = pack
            if self._contains_reference(pack.stickers, sticker):
                return SavedSticker(pack_name, index)
            if len(pack.stickers) >= MAX_PACK_STICKERS:
                continue
            try:
                await self._add(pack_name, user_id, sticker, pack.stickers)
            except TelegramBadRequest as add_error:
                # A concurrent addition may have taken the last place.
                if not sticker_error(add_error, "STICKERS_TOO_MUCH"):
                    raise
                self._before_addition.pop(pack_name, None)
                continue
            return SavedSticker(pack_name, index)
        raise PackSeriesFull

    async def _create(self, name: str, index: int, user_id: int, sticker: UploadedSticker, title: str) -> SavedSticker:
        try:
            self._before_addition[name] = frozenset()
            await self.bot.create_new_sticker_set(
                user_id=user_id,
                name=name,
                title=title,
                stickers=[sticker.input_sticker()],
                sticker_type="regular",
            )
        except TelegramBadRequest as creation_error:
            # A concurrent save may have created this exact pack.
            try:
                pack = await self.bot.get_sticker_set(name)
            except TelegramBadRequest as retry_error:
                if not sticker_error(retry_error, "STICKERSET_INVALID"):
                    raise
                raise creation_error
            self._before_addition.pop(name, None)
            if not self._contains_reference(pack.stickers, sticker):
                await self._add(name, user_id, sticker, pack.stickers)
            return SavedSticker(name, index)
        return SavedSticker(name, index, created=True)

    @staticmethod
    def _same_format(sticker: Sticker, kind: str) -> bool:
        actual = "animated" if sticker.is_animated else "video" if sticker.is_video else "static"
        return actual == kind and sticker.type == "regular"

    async def resolve(self, name: str, uploaded: UploadedSticker) -> str | None:
        """Return a verified pack sticker ID, or None; never fall back to the upload.

        file_unique_id handles reordering and duplicates. Telegram re-encodes
        cross-pack copies with a new identity and bytes, so exactly one new
        same-format entry since this client's own addition identifies them next.
        An exact byte comparison covers the rest. Missing/ambiguous results fall
        back to a pack link at the caller.
        """
        checked: set[str] = set()
        fingerprinted = False
        digest, size = uploaded.sha256, uploaded.size
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
                        if sticker.file_id == uploaded.file_id or unique_id and sticker.file_unique_id == unique_id:
                            return sticker.file_id
                    if (before := self._before_addition.get(name)) is not None:
                        added = [sticker for sticker in candidates if sticker.file_unique_id not in before]
                        if len(added) == 1:
                            return added[0].file_id
                    if digest is None and not fingerprinted:
                        # Cross-pack copies can get a new identity. Read the source
                        # only after the zero-download identity path has failed.
                        fingerprinted = True
                        try:
                            source = await download_by_file_id(uploaded.file_id, self.bot, max_bytes=MAX_STICKER_BYTES)
                        except DownloadTooLarge:
                            source = None
                        if source is not None:
                            with source:
                                payload = source.getvalue()
                            if payload:
                                digest, size = hashlib.sha256(payload).hexdigest(), len(payload)
                    if digest and size is not None and 0 < size <= MAX_STICKER_BYTES:
                        # Order only prioritizes downloads; it never determines the reply.
                        for sticker in reversed(candidates):
                            if len(checked) >= MAX_CONTENT_LOOKUPS:
                                break
                            if sticker.file_size != size or sticker.file_unique_id in checked:
                                continue
                            checked.add(sticker.file_unique_id)
                            try:
                                data = await download_by_file_id(sticker.file_id, self.bot, max_bytes=size)
                            except DownloadTooLarge:
                                continue
                            if data is None:
                                continue
                            with data:
                                payload = data.getvalue()
                            if len(payload) == size and hashlib.sha256(payload).hexdigest() == digest:
                                return sticker.file_id
        except TimeoutError, TelegramAPIError:
            pass
        logger.warning("Saved sticker preview could not be resolved")
        return None
