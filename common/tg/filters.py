import asyncio
import io
import re
import warnings
from enum import IntEnum, auto
from typing import Container, List, Optional, Set, Tuple, Union, Iterable, Callable

from aiogram import types
from aiogram.dispatcher.filters.filters import BoundFilter, Filter
from aiogram.types import CallbackQuery, ChatType, ContentType, Message, Document, PhotoSize, Sticker, Video, Animation, VideoNote
from pydantic import BaseModel

from common.tg.utils import download_text, profile_photo, download
from common.utils import list_get


class SimpleExtractor:
    @classmethod
    async def image(cls, m: Message):
        def doc(d: Document):
            if d.mime_base == 'image':
                if d.mime_subtype.endswith(('jpeg', 'png', 'tiff', 'bmp', 'gif', 'webp')):
                    return d
            return None

        if m.photo:
            return m.photo[-1]
        if m.document:
            return doc(m.document)
        if m.sticker:
            if not (m.sticker.is_animated or m.sticker.is_video):
                return m.sticker

        return None

    @classmethod
    async def video(cls, m: Message):
        if m.sticker and m.sticker.is_video:
            return m.sticker
        return m.video or m.animation or m.video_note

    @classmethod
    async def document(cls, m: Message) -> Optional[Document]:
        return m.document

    @classmethod
    async def profile_photo(cls, m: Message):
        if m.forward_from:
            if pp := await profile_photo(m.forward_from):
                return pp

        return await profile_photo(m.from_user)


class Extractor:
    class ReplyPolicy(IntEnum):
        only_origin = auto()
        prefer_origin = auto()
        prefer_reply = auto()
        only_reply = auto()

    @classmethod
    async def extract_many(cls, message: Message, extractors: Iterable[Tuple[ReplyPolicy, Callable]]):
        pairs = []

        for reply_policy, extractor in extractors:
            if reply_policy == cls.ReplyPolicy.only_origin:
                target = message
                if dest := await extractor(target):
                    pairs.append((target, dest))

            elif reply_policy == cls.ReplyPolicy.prefer_origin:
                target = message
                if dest := await extractor(target):
                    pairs.append((target, dest))

                if message.reply_to_message:
                    target = message.reply_to_message
                    if dest := await extractor(target):
                        pairs.append((target, dest))

            elif reply_policy == cls.ReplyPolicy.prefer_reply:
                if message.reply_to_message:
                    target = message.reply_to_message
                    if dest := await extractor(target):
                        pairs.append((target, dest))

                target = message
                if dest := await extractor(target):
                    pairs.append((target, dest))

            elif reply_policy == cls.ReplyPolicy.only_reply:
                if message.reply_to_message:
                    target = message.reply_to_message
                    if dest := await extractor(target):
                        pairs.append((target, dest))

        return pairs

    @classmethod
    async def extract(cls, message: Message, extractors: Iterable[Tuple[ReplyPolicy, Callable]]):
        result = await cls.extract_many(message, extractors)
        for target, dest in result:
            return target, dest
        return message, None

    @classmethod
    async def image(cls, message: Message,
                    with_reply: bool = True,
                    with_profile_photo: bool = False,
                    with_download: bool = False) -> Tuple[Message, Union[PhotoSize, Document, Sticker, io.BytesIO]]:
        extractors = []

        if with_reply:
            extractors.append((cls.ReplyPolicy.prefer_origin, SimpleExtractor.image))
        else:
            extractors.append((cls.ReplyPolicy.only_origin, SimpleExtractor.image))

        if with_profile_photo:
            if with_reply:
                extractors.append((cls.ReplyPolicy.prefer_reply, SimpleExtractor.profile_photo))
            else:
                extractors.append((cls.ReplyPolicy.only_origin, SimpleExtractor.profile_photo))

        target, dest = await cls.extract(message, extractors)

        if with_download:
            file = await download(dest)
            return target, file

        return target, dest

    @classmethod
    async def two_images(cls, message: Message,
                         with_reply: bool = True,
                         with_profile_photo: bool = True,
                         with_download: bool = False) -> Tuple[Message, Union[PhotoSize, Document, Sticker, io.BytesIO, None],
                                                               Message, Union[PhotoSize, Document, Sticker, io.BytesIO, None]]:
        extractors = []

        if with_reply:
            extractors.append((cls.ReplyPolicy.prefer_origin, SimpleExtractor.image))
        else:
            extractors.append((cls.ReplyPolicy.only_origin, SimpleExtractor.image))

        if with_profile_photo:
            if with_reply:
                extractors.append((cls.ReplyPolicy.prefer_reply, SimpleExtractor.profile_photo))
            else:
                extractors.append((cls.ReplyPolicy.only_origin, SimpleExtractor.profile_photo))

        result = await cls.extract_many(message, extractors)
        if len(result) < 2:
            return message, None, message, None

        (target_1, dest_1), (target_2, dest_2) = result[0], result[1]

        if with_download:
            file_1, file_2 = await asyncio.gather(download(dest_1), download(dest_2))
            return target_1, file_1, target_2, file_2

        return target_1, dest_1, target_2, dest_2

    @classmethod
    async def video(cls, message: Message, with_reply: bool = True, with_download: bool = False) -> Tuple[Message,
                                                                                                          Union[Video, Animation, VideoNote, io.BytesIO]]:
        extractors = []

        if with_reply:
            extractors.append((cls.ReplyPolicy.prefer_origin, SimpleExtractor.video))
        else:
            extractors.append((cls.ReplyPolicy.only_origin, SimpleExtractor.video))

        target, dest = await cls.extract(message, extractors)

        if with_download:
            file = await download(dest)
            return target, file

        return target, dest

    @classmethod
    async def document(cls, message: Message, with_reply: bool = True, with_download: bool = False) -> Tuple[Message, Union[Document, io.BytesIO]]:
        extractors = []

        if with_reply:
            extractors.append((cls.ReplyPolicy.prefer_origin, SimpleExtractor.document))
        else:
            extractors.append((cls.ReplyPolicy.only_origin, SimpleExtractor.document))

        target, dest = await cls.extract(message, extractors)

        if with_download:
            file = await download(dest)
            return target, file

        return target, dest

    @classmethod
    async def custom(cls, message: Message, extractors: Iterable[Tuple[ReplyPolicy, Callable]], with_download: bool = False):
        target, dest = await cls.extract(message, extractors)
        if with_download:
            file = await download(dest)
            return target, file
        return target, dest


class MetaInfo(BaseModel):
    class Config:
        arbitrary_types_allowed = True
        anystr_strip_whitespace = True
        validate_assignment = True

    message: Message
    command: str = ''
    hashtag: str = ''
    arguments: List[str]
    text: str

    @property
    def keyword(self) -> str:
        return self.command or self.hashtag

    def reply(self) -> Message:
        if self.text or self.message.content_type != ContentType.TEXT:
            return self.message

        if self.message.reply_to_message:
            return self.message.reply_to_message

        return self.message

    def extract_text(self) -> Tuple[Message, str]:
        return self._extract_text()

    def extract_text_with_doc(self) -> Tuple[Message, str, Document]:
        return self._extract_text(with_doc=True)

    async def extract_text_with_doc_plain(self) -> Tuple[Message, str]:
        target, text, doc = self.extract_text_with_doc()
        if not text:
            if not doc:
                return target, ''
            text = await download_text(doc.file_id)
        return target, text or ''

    def _extract_text(self, with_doc: bool = False) -> Union[Tuple[Message, str], Tuple[Message, str, Document]]:
        def extract_doc(d: Document) -> Optional[Document]:
            if d and d.mime_type.partition('/')[0] == 'text':
                return d
            return None

        target = self.message
        text, doc = None, None

        if with_doc:
            doc = extract_doc(target.document)

        if not (text or doc):
            text = self.text

        if not (text or doc):
            if self.message.reply_to_message:
                target = self.message.reply_to_message

                if with_doc:
                    doc = extract_doc(target.document)

                if not (text or doc):
                    text = target.text or target.caption or ''

        # If no text, target is a reply (if exists)
        if with_doc:
            return target, text, doc
        return target, text

    async def extract_image(self, with_reply: bool = True, with_profile_photo: bool = False) -> Tuple[Message, Union[PhotoSize, Document, Sticker]]:
        return await Extractor.image(self.message, with_reply, with_profile_photo)

    async def extract_image_with_downloading(self, with_reply: bool = True, with_profile_photo: bool = False) -> Tuple[Message, Optional[io.BytesIO]]:
        return await Extractor.image(self.message, with_reply, with_profile_photo, with_download=True)

    async def extract_two_images(self) -> Tuple[Message, Union[PhotoSize, Document, Sticker], Message, Union[PhotoSize, Document, Sticker]]:
        return await Extractor.two_images(self.message)

    async def extract_video(self, with_reply: bool = True) -> Tuple[Message, Union[Video, Animation, VideoNote, Sticker]]:
        return await Extractor.video(self.message, with_reply)

    async def extract_video_with_downloading(self, with_reply: bool = True) -> Tuple[Message, Optional[io.BytesIO]]:
        return await Extractor.video(self.message, with_reply, with_download=True)

    async def extract_doc(self, with_reply: bool = True) -> Tuple[Message, Document]:
        return await Extractor.document(self.message, with_reply)


class MetaCommand(Filter):
    def __init__(self, *keywords: str, args: int = None):
        self.args = args
        self.prefixes = '/'
        self.ignore_case = True
        self.ignore_mention = False
        self.ignore_caption = False

        self.commands = tuple(k.lower() for k in keywords) if self.ignore_case else keywords
        self.hashtags = tuple(f'#{c}' for c in self.commands)

        p = '|'.join(f'(?:{k})' for k in keywords)
        self.hashtags_pattern = re.compile(f'#\\b({p})((?:_[\\w\\d]*)*)\\b', re.IGNORECASE)

    async def check(self, message: types.Message):
        text = message.text or (message.caption if not self.ignore_caption else None)
        if not text or not text.strip():
            return False
        me = await message.bot.me

        def check_command(t: str) -> Optional[MetaInfo]:
            split = t.split()
            full_command = split[0]
            prefix, (command, _, mention) = full_command[0], full_command[1:].partition('@')

            if not self.ignore_mention and mention and me.username.lower() != mention.lower():
                return None
            if prefix not in self.prefixes:
                return None
            if (command.lower() if self.ignore_case else command) not in self.commands:
                return None

            if self.args is None:
                arguments = split[1:]
                text = t.lstrip()[len(full_command):]
            else:
                firsts = 1 + self.args
                arguments = split[1:firsts]
                texts = t.split(maxsplit=firsts)
                text = list_get(texts, firsts, '')

            return MetaInfo(message=message, command=command, arguments=arguments, text=text)

        def check_hashtag(t: str) -> Optional[MetaInfo]:
            match = self.hashtags_pattern.search(t)
            if match:
                h, args = match.groups()
                arguments = [arg for arg in args.split('_') if arg][:self.args]
                return MetaInfo(message=message, hashtag=h, arguments=arguments, text=t[:match.start()] + t[match.end():])

            return None

        meta = check_command(text) or check_hashtag(text)
        if meta is None:
            return False
        message.conf['meta'] = meta
        return {'meta': meta}


class ChatTypeFilter(BoundFilter):
    key = 'chat_type'

    def __init__(self, chat_type: Container[ChatType]):
        if isinstance(chat_type, str):
            chat_type = {chat_type}

        self.chat_type: Set[str] = set(chat_type)

    async def check(self, obj: Union[Message, CallbackQuery]):
        if isinstance(obj, Message):
            obj = obj.chat
        elif isinstance(obj, CallbackQuery):
            obj = obj.message.chat
        else:
            warnings.warn("ChatTypeFilter doesn't support %s as input", type(obj))
            return False

        return obj.type in self.chat_type
