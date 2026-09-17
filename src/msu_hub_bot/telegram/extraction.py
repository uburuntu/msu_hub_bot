"""Ordered origin, reply and profile-photo selection for command inputs."""

from collections.abc import Awaitable, Callable, Iterable
from enum import IntEnum, auto
from typing import TypeVar

from aiogram.types import Animation, Document, Message, MessageOriginUser, PhotoSize, Sticker, Video, VideoNote

from msu_hub_bot.telegram.context import bot_for

ImageMedia = PhotoSize | Document | Sticker
VideoMedia = Video | Animation | VideoNote | Sticker
T = TypeVar("T")


class SimpleExtractor:
    @classmethod
    async def image(cls, message: Message) -> ImageMedia | None:
        if message.photo:
            return message.photo[-1]
        if document := message.document:
            base, _, subtype = (document.mime_type or "").partition("/")
            if base == "image" and subtype.endswith(("jpeg", "png", "tiff", "bmp", "gif", "webp")):
                return document
        if sticker := message.sticker:
            if not (sticker.is_animated or sticker.is_video):
                return sticker
        return None

    @classmethod
    async def video(cls, message: Message) -> VideoMedia | None:
        if message.sticker and message.sticker.is_video:
            return message.sticker
        return message.video or message.animation or message.video_note

    @classmethod
    async def document(cls, message: Message) -> Document | None:
        return message.document

    @classmethod
    async def profile_photo(cls, message: Message) -> PhotoSize | None:
        users = []
        if isinstance(message.forward_origin, MessageOriginUser):
            users.append(message.forward_origin.sender_user)
        if message.from_user:
            users.append(message.from_user)
        for user in users:
            photos = await bot_for(message).get_user_profile_photos(user_id=user.id, limit=1)
            if photos.photos and photos.photos[0]:
                return photos.photos[0][-1]
        return None


class Extractor:
    class ReplyPolicy(IntEnum):
        only_origin = auto()
        prefer_origin = auto()
        prefer_reply = auto()
        only_reply = auto()

    @classmethod
    def targets(cls, message: Message, policy: ReplyPolicy) -> list[Message]:
        reply = [message.reply_to_message] if message.reply_to_message else []
        if policy == cls.ReplyPolicy.only_origin:
            return [message]
        if policy == cls.ReplyPolicy.prefer_origin:
            return [message, *reply]
        if policy == cls.ReplyPolicy.prefer_reply:
            return [*reply, message]
        return reply

    @classmethod
    async def extract_many(
        cls, message: Message, extractors: Iterable[tuple[ReplyPolicy, Callable[[Message], Awaitable[T | None]]]]
    ) -> list[tuple[Message, T]]:
        pairs: list[tuple[Message, T]] = []
        for policy, extractor in extractors:
            for target in cls.targets(message, policy):
                result = await extractor(target)
                if result is not None:
                    pairs.append((target, result))
        return pairs

    @classmethod
    async def extract(
        cls, message: Message, extractors: Iterable[tuple[ReplyPolicy, Callable[[Message], Awaitable[T | None]]]]
    ) -> tuple[Message, T | None]:
        for policy, extractor in extractors:
            for target in cls.targets(message, policy):
                result = await extractor(target)
                if result is not None:
                    return target, result
        return message, None

    @classmethod
    def image_extractors(
        cls, with_reply: bool, with_profile_photo: bool
    ) -> list[tuple[ReplyPolicy, Callable[[Message], Awaitable[ImageMedia | None]]]]:
        policy = cls.ReplyPolicy.prefer_origin if with_reply else cls.ReplyPolicy.only_origin
        choices: list[tuple[Extractor.ReplyPolicy, Callable[[Message], Awaitable[ImageMedia | None]]]] = [(policy, SimpleExtractor.image)]
        if with_profile_photo:
            policy = cls.ReplyPolicy.prefer_reply if with_reply else cls.ReplyPolicy.only_origin
            choices.append((policy, SimpleExtractor.profile_photo))
        return choices

    @classmethod
    async def image(cls, message: Message, with_reply: bool = True, with_profile_photo: bool = False) -> tuple[Message, ImageMedia | None]:
        return await cls.extract(message, cls.image_extractors(with_reply, with_profile_photo))

    @classmethod
    async def two_images(
        cls, message: Message, with_reply: bool = True, with_profile_photo: bool = True
    ) -> tuple[Message, ImageMedia | None, Message, ImageMedia | None]:
        pairs: list[tuple[Message, ImageMedia]] = await cls.extract_many(message, cls.image_extractors(with_reply, with_profile_photo))
        if len(pairs) < 2:
            return message, None, message, None
        return pairs[0][0], pairs[0][1], pairs[1][0], pairs[1][1]

    @classmethod
    async def video(cls, message: Message, with_reply: bool = True) -> tuple[Message, VideoMedia | None]:
        policy = cls.ReplyPolicy.prefer_origin if with_reply else cls.ReplyPolicy.only_origin
        return await cls.extract(message, [(policy, SimpleExtractor.video)])

    @classmethod
    async def document(cls, message: Message, with_reply: bool = True) -> tuple[Message, Document | None]:
        policy = cls.ReplyPolicy.prefer_origin if with_reply else cls.ReplyPolicy.only_origin
        return await cls.extract(message, [(policy, SimpleExtractor.document)])
