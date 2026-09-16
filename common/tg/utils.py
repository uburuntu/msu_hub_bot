"""Telegram presentation, linking and input helpers shared by commands."""

from collections.abc import Callable, Iterable
from contextlib import suppress

from aiogram import Bot
from aiogram.enums import ChatAction, ChatType, ContentType, MessageEntityType
from aiogram.exceptions import TelegramAPIError
from aiogram.types import Chat, ChatFullInfo, InputMediaPhoto, InputMediaVideo, Message, PhotoSize, TelegramObject, Update, User
from aiogram.utils.markdown import hide_link, hlink
from yarl import URL

from common.tg.context import bot_for
from common.tg.delivery import ReplyTarget, send_album
from common.tg.extraction import Extractor, ImageMedia
from common.tg.files import download as download
from common.tg.files import download_by_file_id as download_by_file_id
from common.tg.files import download_text as download_text
from common.utils import cut_long_text, one_liner


async def send_super_reply(
    message: Message,
    text: str,
    web_preview: str | None = None,
    photos_urls: Iterable[str] | None = None,
    video_urls: Iterable[str] | None = None,
    text_postprocess: Callable[[str], str] | None = None,
) -> Message | None:
    target = ReplyTarget.from_message(message)
    return await send_super_message(
        bot_for(message),
        text,
        web_preview,
        photos_urls,
        video_urls,
        target.chat_id,
        target.message_id,
        text_postprocess,
        message_thread_id=target.thread_id,
    )


async def send_super_message(
    bot: Bot,
    text: str,
    web_preview: str | None,
    photos_urls: Iterable[str] | None,
    video_urls: Iterable[str] | None,
    chat_id: int,
    reply_to: int | None = None,
    text_postprocess: Callable[[str], str] | None = None,
    *,
    message_thread_id: int | None = None,
) -> Message | None:
    message = None
    process_text = text_postprocess or (lambda value: value)
    texts = [process_text(part) for part in cut_long_text(text)] if text else []
    for index, part in enumerate(texts):
        last = index == len(texts) - 1
        preview = web_preview if last else None
        reply = ReplyTarget(chat_id, reply_to).parameters() if reply_to is not None else None
        message = await bot.send_message(
            chat_id=chat_id,
            text=(hide_link(preview) if preview else "") + part,
            disable_web_page_preview=not preview,
            reply_parameters=reply,
            message_thread_id=message_thread_id,
        )
        reply_to = message.message_id
    reply = ReplyTarget(chat_id, reply_to).parameters() if reply_to is not None else None
    if photos_urls:
        await send_album(
            bot, chat_id, [InputMediaPhoto(media=url) for url in photos_urls], reply_parameters=reply, message_thread_id=message_thread_id
        )
    if video_urls:
        await send_album(
            bot, chat_id, [InputMediaVideo(media=url) for url in video_urls], reply_parameters=reply, message_thread_id=message_thread_id
        )
    return message


def extract_urls(message: Message, include_text_link: bool = True) -> list[tuple[URL, MessageEntityType]]:
    urls = []
    text = message.text or message.caption or ""
    entities = message.entities or message.caption_entities or []
    for entity in entities:
        url_text = None
        if entity.type == MessageEntityType.TEXT_LINK and include_text_link:
            url_text = entity.url
        elif entity.type == MessageEntityType.URL:
            url_text = entity.extract_from(text)
        if url_text:
            url = URL(url_text)
            if not url.scheme:
                url = URL("https://" + url_text)
            if url.host:
                urls.append((url, MessageEntityType(entity.type)))
    return urls


def user_info(user: User, sender_chat: Chat | None = None) -> str:
    if sender_chat:
        return chat_info(sender_chat)
    last_name = " " + user.last_name if user.last_name else ""
    username = ", @" + user.username if user.username else ""
    language_code = ", " + user.language_code if user.language_code else ""
    return f"{user.first_name}{last_name} ({user.id}{username}{language_code})"


def chat_info(chat: Chat) -> str:
    if chat.type == ChatType.PRIVATE:
        return "private"
    username = ", @" + chat.username if chat.username else ""
    return f"{chat.type} | {chat.title} ({chat.id}{username})"


def message_info(message: Message) -> str:
    prefix = f"{message.message_id} | "
    if message.text:
        return prefix + one_liner(message.text, cut_len=50)
    return prefix + f"type: {message.content_type}"


def decompose_update(update: Update) -> tuple[TelegramObject, User | None, Chat | None, Chat | None, str]:
    message = update.message or update.edited_message or update.channel_post or update.edited_channel_post
    if message:
        info = message_info(message)
        if update.edited_message or update.edited_channel_post:
            info += " [edited]"
        return message, message.from_user, message.sender_chat, message.chat, info
    callback = update.callback_query
    if callback:
        return callback, callback.from_user, None, callback.message.chat if callback.message else None, callback.data or ""
    inline = update.inline_query or update.chosen_inline_result
    if inline:
        return inline, inline.from_user, None, None, one_liner(inline.query, cut_len=50)
    member = update.chat_member or update.my_chat_member
    if member:
        info = f"{user_info(member.new_chat_member.user)}: {member.old_chat_member.status} -> {member.new_chat_member.status}"
        return member, member.from_user, None, member.chat, info
    checkout = update.shipping_query or update.pre_checkout_query
    if checkout:
        return checkout, checkout.from_user, None, None, checkout.model_dump_json()
    if update.poll_answer:
        answer = update.poll_answer
        return answer, answer.user, None, None, f"{answer.option_ids} ({answer.poll_id})"
    if update.poll:
        poll = update.poll
        return poll, None, None, None, f"{one_liner(poll.question, cut_len=50)} ({poll.id})"
    return update, None, None, None, update.model_dump_json()


def parse_update(update: Update) -> tuple[TelegramObject, str | None, str | None, str]:
    event, user, sender_chat, chat, info = decompose_update(update)
    return event, user_info(user, sender_chat) if user else None, chat_info(chat) if chat else None, info


def username_link(user: User) -> str:
    return "@" + user.username if user.username else user.mention_html()


def username_mention(user: User) -> str:
    return user.mention_html("@" + user.username if user.username else None)


def user_mention(user_id: int, name: str) -> str:
    return hlink(str(name), f"tg://user?id={user_id}")


async def chat_url(chat: Chat, force_link: bool = False) -> str | None:
    if chat.type == ChatType.PRIVATE:
        return f"tg://user?id={chat.id}"
    if chat.username:
        return f"https://t.me/{chat.username}"
    if force_link:
        full = chat if isinstance(chat, ChatFullInfo) else await bot_for(chat).get_chat(chat.id)
        return full.invite_link
    return None


async def chat_link(chat: Chat, force_link: bool = False) -> str:
    url = await chat_url(chat, force_link)
    return hlink(chat.full_name, url) if url else chat.full_name


def sender_mention(message: Message) -> str:
    if chat := message.sender_chat:
        return hlink(chat.full_name, f"https://t.me/{chat.username}") if chat.username else chat.full_name
    return message.from_user.mention_html() if message.from_user else ""


async def send_message_copy(message: Message, chat_id: int, disable_notification: bool = False) -> bool:
    return await send_message_copy_raw(bot_for(message), chat_id, message.chat.id, message.message_id, disable_notification)


async def send_message_copy_raw(bot: Bot, chat_id: int, from_chat_id: int, message_id: int, disable_notification: bool = False) -> bool:
    with suppress(TelegramAPIError):
        await bot.copy_message(chat_id, from_chat_id, message_id, disable_notification=disable_notification)
        return True
    return False


async def profile_photo(user: User) -> PhotoSize | None:
    photos = await user.get_profile_photos(limit=1)
    return photos.photos[0][-1] if photos.photos and photos.photos[0] else None


async def extract_image(message: Message, with_reply: bool = True, with_profile_photo: bool = False) -> tuple[Message, ImageMedia | None]:
    return await Extractor.image(message, with_reply, with_profile_photo)


def action_by_type(content_type: str) -> str | None:
    if content_type in (ContentType.TEXT, ContentType.STICKER, ContentType.POLL, ContentType.DICE):
        return ChatAction.TYPING
    if content_type in (ContentType.AUDIO, ContentType.VOICE):
        return ChatAction.UPLOAD_VOICE
    if content_type == ContentType.DOCUMENT:
        return ChatAction.UPLOAD_DOCUMENT
    if content_type in (ContentType.ANIMATION, ContentType.VIDEO):
        return ChatAction.UPLOAD_VIDEO
    if content_type in (ContentType.PHOTO, "list[photo]"):
        return ChatAction.UPLOAD_PHOTO
    if content_type == ContentType.VIDEO_NOTE:
        return ChatAction.UPLOAD_VIDEO_NOTE
    if content_type in (ContentType.LOCATION, ContentType.VENUE):
        return ChatAction.FIND_LOCATION
    return None
