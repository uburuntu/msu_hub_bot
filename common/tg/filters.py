"""Command filters enrich dispatch data without mutating Telegram models."""

import io
from collections.abc import Container
from dataclasses import dataclass, field
from typing import Any

from aiogram import Bot
from aiogram.enums import ContentType
from aiogram.filters import Command, Filter
from aiogram.types import CallbackQuery, Document, Message

from common.tg.command import CommandParser
from common.tg.context import bot_for
from common.tg.extraction import Extractor as Extractor
from common.tg.extraction import ImageMedia, VideoMedia
from common.tg.extraction import SimpleExtractor as SimpleExtractor
from common.tg.files import download, download_text


@dataclass(slots=True)
class MetaInfo:
    message: Message
    command: str = ""
    hashtag: str = ""
    arguments: list[str] = field(default_factory=list)
    text: str = ""

    def __post_init__(self) -> None:
        self.command = self.command.strip()
        self.hashtag = self.hashtag.strip()
        self.arguments = [argument.strip() for argument in self.arguments]
        self.text = self.text.strip()

    @property
    def keyword(self) -> str:
        return self.command or self.hashtag

    def reply(self) -> Message:
        if self.text or self.message.content_type != ContentType.TEXT:
            return self.message
        return self.message.reply_to_message or self.message

    def extract_text(self) -> tuple[Message, str]:
        target, text, _ = self._extract_text(with_doc=False)
        return target, text

    def extract_text_with_doc(self) -> tuple[Message, str, Document | None]:
        return self._extract_text(with_doc=True)

    async def extract_text_with_doc_plain(self) -> tuple[Message, str]:
        target, text, document = self.extract_text_with_doc()
        if not text and document:
            text = await download_text(document.file_id, bot_for(self.message)) or ""
        return target, text

    def _extract_text(self, *, with_doc: bool) -> tuple[Message, str, Document | None]:
        def text_document(message: Message) -> Document | None:
            document = message.document
            if with_doc and document and (document.mime_type or "").partition("/")[0] == "text":
                return document
            return None

        target = self.message
        document = text_document(target)
        text = "" if document else self.text
        if not (text or document) and self.message.reply_to_message:
            target = self.message.reply_to_message
            document = text_document(target)
            text = "" if document else target.text or target.caption or ""
        return target, text, document

    async def extract_image(self, with_reply: bool = True, with_profile_photo: bool = False) -> tuple[Message, ImageMedia | None]:
        return await Extractor.image(self.message, with_reply, with_profile_photo)

    async def extract_image_with_downloading(
        self, with_reply: bool = True, with_profile_photo: bool = False
    ) -> tuple[Message, io.BytesIO | None]:
        target, media = await self.extract_image(with_reply, with_profile_photo)
        return target, await download(media, self.message.bot)

    async def extract_two_images(self) -> tuple[Message, ImageMedia | None, Message, ImageMedia | None]:
        return await Extractor.two_images(self.message)

    async def extract_video(self, with_reply: bool = True) -> tuple[Message, VideoMedia | None]:
        return await Extractor.video(self.message, with_reply)

    async def extract_video_with_downloading(self, with_reply: bool = True) -> tuple[Message, io.BytesIO | None]:
        target, media = await self.extract_video(with_reply)
        return target, await download(media, self.message.bot)

    async def extract_doc(self, with_reply: bool = True) -> tuple[Message, Document | None]:
        return await Extractor.document(self.message, with_reply)


class MetaCommand(Filter):
    def __init__(self, *keywords: str, args: int | None = None) -> None:
        self.parser = CommandParser(*keywords, args=args)
        self.commands = self.parser.commands
        self.args = args

    async def __call__(self, message: Message, bot: Bot) -> bool | dict[str, Any]:
        text = message.text or message.caption
        username = None
        if text and text.strip() and "@" in text.split(maxsplit=1)[0]:
            username = (await bot.me()).username
        parsed = self.parser.parse(text, username=username)
        if parsed is None:
            return False
        return {
            "meta": MetaInfo(
                message=message,
                command=parsed.command,
                hashtag=parsed.hashtag,
                arguments=list(parsed.arguments),
                text=parsed.text,
            )
        }


class ChatTypeFilter(Filter):
    def __init__(self, chat_type: Container[str] | str) -> None:
        self.chat_type = {chat_type} if isinstance(chat_type, str) else chat_type

    async def __call__(self, event: Message | CallbackQuery) -> bool:
        if isinstance(event, Message):
            return event.chat.type in self.chat_type
        return event.message is not None and event.message.chat.type in self.chat_type


class SlashCommand(Command):
    """Plain command registrations ignore case and never match captions."""

    def __init__(self, *commands: str) -> None:
        super().__init__(*commands, ignore_case=True, ignore_mention=False)

    async def __call__(self, message: Message, bot: Bot) -> bool | dict[str, Any]:
        if message.text is None:
            return False
        return await super().__call__(message, bot)
