"""Reply targets and album boundaries shared by feature handlers."""

from collections.abc import Sequence
from dataclasses import dataclass

from aiogram import Bot
from aiogram.types import InputMediaAudio, InputMediaDocument, InputMediaPhoto, InputMediaVideo, Message, ReplyParameters
from aiogram.types import InputMediaLivePhoto

from common.tg.context import bot_for

AlbumMedia = InputMediaAudio | InputMediaDocument | InputMediaPhoto | InputMediaVideo


@dataclass(frozen=True, slots=True)
class ReplyTarget:
    chat_id: int
    message_id: int
    thread_id: int | None = None

    @classmethod
    def from_message(cls, message: Message) -> "ReplyTarget":
        return cls(message.chat.id, message.message_id, message.message_thread_id if message.is_topic_message else None)

    def parameters(self, *, allow_missing: bool | None = None) -> ReplyParameters:
        return ReplyParameters(message_id=self.message_id, allow_sending_without_reply=allow_missing)


async def send_album(
    bot: Bot,
    chat_id: int,
    media: Sequence[AlbumMedia],
    *,
    reply_parameters: ReplyParameters | None = None,
    message_thread_id: int | None = None,
) -> list[Message]:
    """Telegram albums contain 2–10 items; a remaining single item is sent directly."""
    sent: list[Message] = []
    for start in range(0, len(media), 10):
        batch = list(media[start : start + 10])
        if len(batch) > 1:
            album: list[AlbumMedia | InputMediaLivePhoto] = list(batch)
            sent.extend(
                await bot.send_media_group(
                    chat_id=chat_id, media=album, reply_parameters=reply_parameters, message_thread_id=message_thread_id
                )
            )
            continue
        item = batch[0]
        if isinstance(item, InputMediaPhoto):
            message = await bot.send_photo(
                chat_id=chat_id,
                photo=item.media,
                caption=item.caption,
                parse_mode=item.parse_mode,
                caption_entities=item.caption_entities,
                has_spoiler=item.has_spoiler,
                show_caption_above_media=item.show_caption_above_media,
                reply_parameters=reply_parameters,
                message_thread_id=message_thread_id,
            )
        elif isinstance(item, InputMediaVideo):
            message = await bot.send_video(
                chat_id=chat_id,
                video=item.media,
                thumbnail=item.thumbnail,
                width=item.width,
                height=item.height,
                duration=item.duration,
                supports_streaming=item.supports_streaming,
                caption=item.caption,
                parse_mode=item.parse_mode,
                caption_entities=item.caption_entities,
                has_spoiler=item.has_spoiler,
                show_caption_above_media=item.show_caption_above_media,
                reply_parameters=reply_parameters,
                message_thread_id=message_thread_id,
            )
        elif isinstance(item, InputMediaDocument):
            message = await bot.send_document(
                chat_id=chat_id,
                document=item.media,
                thumbnail=item.thumbnail,
                caption=item.caption,
                parse_mode=item.parse_mode,
                caption_entities=item.caption_entities,
                disable_content_type_detection=item.disable_content_type_detection,
                reply_parameters=reply_parameters,
                message_thread_id=message_thread_id,
            )
        else:
            message = await bot.send_audio(
                chat_id=chat_id,
                audio=item.media,
                thumbnail=item.thumbnail,
                duration=item.duration,
                performer=item.performer,
                title=item.title,
                caption=item.caption,
                parse_mode=item.parse_mode,
                caption_entities=item.caption_entities,
                reply_parameters=reply_parameters,
                message_thread_id=message_thread_id,
            )
        sent.append(message)
    return sent


async def reply_album(message: Message, media: Sequence[AlbumMedia]) -> list[Message]:
    target = ReplyTarget.from_message(message)
    return await send_album(
        bot_for(message), target.chat_id, media, reply_parameters=target.parameters(), message_thread_id=target.thread_id
    )
