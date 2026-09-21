"""Command filters enrich dispatch data without mutating Telegram models."""

import io
from collections.abc import Container, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, overload

from aiogram import Bot
from aiogram.enums import ContentType
from aiogram.filters import Command, Filter
from aiogram.types import CallbackQuery, Document, Message, MessageEntity, ReplyMarkupUnion
from aiogram.utils.formatting import Text

from msu_hub_bot.telegram.command import CommandParser
from msu_hub_bot.telegram.context import bot_for
from msu_hub_bot.telegram.extraction import Extractor as Extractor
from msu_hub_bot.telegram.extraction import ImageMedia, VideoMedia
from msu_hub_bot.telegram.extraction import SimpleExtractor as SimpleExtractor
from msu_hub_bot.telegram.files import download, download_text
from msu_hub_bot.telegram.rich_input import rich_media, rich_text

if TYPE_CHECKING:
    from msu_hub_bot.telegram.responses import MediaSource, ResponsePolicy


@dataclass(slots=True)
class MetaInfo:
    message: Message
    command: str = ""
    hashtag: str = ""
    arguments: list[str] = field(default_factory=list)
    text: str = ""
    raw_text: str = ""
    context_messages: int = 0
    resolved: dict[str, object] = field(default_factory=dict)
    input_sources: dict[str, Message] = field(default_factory=dict)
    _response_policy: ResponsePolicy | None = field(default=None, repr=False)
    _response_target: Message | None = field(default=None, repr=False)
    _text_input: tuple[Message, str, Document | None] | None = field(default=None, repr=False)
    _image_input: tuple[Message, ImageMedia | None] | None = field(default=None, repr=False)
    _video_input: tuple[Message, VideoMedia | None] | None = field(default=None, repr=False)
    _document_input: tuple[Message, Document | None] | None = field(default=None, repr=False)
    _downloads: dict[str, io.BytesIO] = field(default_factory=dict, repr=False)
    _input_limits: dict[str, int] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self.command = self.command.strip()
        self.hashtag = self.hashtag.strip()
        self.arguments = [argument.strip() for argument in self.arguments]
        self.text = self.text.strip()
        if not self.raw_text:
            self.raw_text = self.text

    @property
    def keyword(self) -> str:
        return self.command or self.hashtag

    def reply_target(self) -> Message:
        if self._response_target is not None:
            return self._response_target
        if self.text or self.message.content_type != ContentType.TEXT:
            return self.message
        return self.message.reply_to_message or self.message

    @overload
    async def reply(
        self,
        text: str | Text | None = None,
        *,
        photo: MediaSource | None = None,
        video: MediaSource | None = None,
        document: MediaSource | None = None,
        audio: MediaSource | None = None,
        entities: Sequence[MessageEntity] | None = None,
        reply_markup: ReplyMarkupUnion | None = None,
        fixed: Literal[True],
        to: Message | None = None,
        width: int | None = None,
        height: int | None = None,
        duration: int | None = None,
        supports_streaming: bool | None = None,
        allow_sending_without_reply: bool = False,
        allow_remote_media: bool = False,
        request_timeout: int | None = None,
    ) -> Message: ...

    @overload
    async def reply(
        self,
        text: str | Text | None = None,
        *,
        photo: MediaSource | None = None,
        video: MediaSource | None = None,
        document: MediaSource | None = None,
        audio: MediaSource | None = None,
        entities: Sequence[MessageEntity] | None = None,
        reply_markup: ReplyMarkupUnion | None = None,
        fixed: Literal[False] = False,
        to: Message | None = None,
        width: int | None = None,
        height: int | None = None,
        duration: int | None = None,
        supports_streaming: bool | None = None,
        allow_sending_without_reply: bool = False,
        allow_remote_media: bool = False,
        request_timeout: int | None = None,
    ) -> list[Message]: ...

    @overload
    async def reply(
        self,
        text: str | Text | None = None,
        *,
        photo: MediaSource | None = None,
        video: MediaSource | None = None,
        document: MediaSource | None = None,
        audio: MediaSource | None = None,
        entities: Sequence[MessageEntity] | None = None,
        reply_markup: ReplyMarkupUnion | None = None,
        fixed: bool,
        to: Message | None = None,
        width: int | None = None,
        height: int | None = None,
        duration: int | None = None,
        supports_streaming: bool | None = None,
        allow_sending_without_reply: bool = False,
        allow_remote_media: bool = False,
        request_timeout: int | None = None,
    ) -> Message | list[Message]: ...

    async def reply(
        self,
        text: str | Text | None = None,
        *,
        photo: MediaSource | None = None,
        video: MediaSource | None = None,
        document: MediaSource | None = None,
        audio: MediaSource | None = None,
        entities: Sequence[MessageEntity] | None = None,
        reply_markup: ReplyMarkupUnion | None = None,
        fixed: bool = False,
        to: Message | None = None,
        width: int | None = None,
        height: int | None = None,
        duration: int | None = None,
        supports_streaming: bool | None = None,
        allow_sending_without_reply: bool = False,
        allow_remote_media: bool = False,
        request_timeout: int | None = None,
    ) -> Message | list[Message]:
        """Send prepared output using this invocation's policy and selected input source."""
        from msu_hub_bot.telegram.responses import ResponsePolicy, send_response

        return await send_response(
            to or self.reply_target(),
            text,
            policy=self._response_policy or ResponsePolicy(),
            photo=photo,
            video=video,
            document=document,
            audio=audio,
            entities=entities,
            reply_markup=reply_markup,
            fixed=fixed,
            width=width,
            height=height,
            duration=duration,
            supports_streaming=supports_streaming,
            allow_sending_without_reply=allow_sending_without_reply,
            allow_remote_media=allow_remote_media,
            request_timeout=request_timeout,
        )

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
        if self._text_input is not None:
            return self._text_input

        def text_document(message: Message) -> Document | None:
            if with_doc:
                documents = (message.document,) if message.document else rich_media(message)
                for document in documents:
                    if isinstance(document, Document) and (document.mime_type or "").partition("/")[0] == "text":
                        return document
            return None

        target = self.message
        document = text_document(target)
        text = "" if document else self.text
        if not (text or document) and self.message.reply_to_message:
            target = self.message.reply_to_message
            document = text_document(target)
            text = "" if document else target.text or target.caption or rich_text(target)
        return target, text, document

    async def extract_image(self, with_reply: bool = True, with_profile_photo: bool = False) -> tuple[Message, ImageMedia | None]:
        if self._image_input is not None:
            return self._image_input
        return await Extractor.image(self.message, with_reply, with_profile_photo)

    async def extract_image_with_downloading(
        self, with_reply: bool = True, with_profile_photo: bool = False
    ) -> tuple[Message, io.BytesIO | None]:
        target, media = await self.extract_image(with_reply, with_profile_photo)
        if media is None:
            return target, None
        if media is not None and media.file_id in self._downloads:
            stream = self._downloads[media.file_id]
            stream.seek(0)
            return target, stream
        return target, await download(media, bot_for(self.message), max_bytes=self._input_limits.get(media.file_id))

    async def extract_two_images(self) -> tuple[Message, ImageMedia | None, Message, ImageMedia | None]:
        return await Extractor.two_images(self.message)

    async def extract_video(self, with_reply: bool = True) -> tuple[Message, VideoMedia | None]:
        if self._video_input is not None:
            return self._video_input
        return await Extractor.video(self.message, with_reply)

    async def extract_video_with_downloading(self, with_reply: bool = True) -> tuple[Message, io.BytesIO | None]:
        target, media = await self.extract_video(with_reply)
        if media is None:
            return target, None
        if media is not None and media.file_id in self._downloads:
            stream = self._downloads[media.file_id]
            stream.seek(0)
            return target, stream
        return target, await download(media, bot_for(self.message), max_bytes=self._input_limits.get(media.file_id))

    async def extract_doc(self, with_reply: bool = True) -> tuple[Message, Document | None]:
        if self._document_input is not None:
            return self._document_input
        return await Extractor.document(self.message, with_reply)


class MetaCommand(Filter):
    def __init__(self, *keywords: str, args: int | None = None) -> None:
        self.parser = CommandParser(*keywords, args=args)
        self.raw_parser = self.parser if args is None else CommandParser(*keywords)
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
        raw = parsed if self.args is None else self.raw_parser.parse(text, username=username)
        return {
            "meta": MetaInfo(
                message=message,
                command=parsed.command,
                hashtag=parsed.hashtag,
                arguments=list(parsed.arguments),
                text=parsed.text,
                raw_text=raw.text if raw is not None else parsed.text,
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
