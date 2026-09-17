import re
from contextlib import suppress

from aiogram import html
from aiogram.exceptions import TelegramBadRequest
import transliterate
from aiogram.types import (
    Message,
    User,
    Chat,
    MessageId,
    MessageOriginUser,
    MessageOriginChat,
    MessageOriginChannel,
    MessageOriginHiddenUser,
    ReplyParameters,
)
from aiogram.utils.markdown import hcode, hbold, hpre, hlink

from msu_hub_bot.telegram.filters import MetaInfo
from msu_hub_bot.telegram.context import bot_for
from msu_hub_bot.telegram.utils import username_link, chat_url, chat_link


async def process_me(message: Message, meta: MetaInfo) -> Message | bool | None:
    _, text = meta.extract_text()
    if not text:
        return True

    with suppress(TelegramBadRequest):
        await message.delete()

    return await message.answer(
        f"{username_link(message.from_user) if message.from_user else await chat_link(message.chat)} {html.quote(text)}"
    )


async def process_copy(message: Message) -> MessageId:
    target = message.reply_to_message or message
    return await bot_for(message).copy_message(
        message.chat.id,
        target.chat.id,
        target.message_id,
        message_thread_id=message.message_thread_id,
        reply_parameters=ReplyParameters(message_id=message.message_id),
    )


async def process_transliterate(_message: Message, meta: MetaInfo) -> Message | bool | None:
    target, text = meta.extract_text()
    if not text:
        return True

    lang = transliterate.detect_language(text, heavy_check=True) or "ru"
    text = transliterate.translit(text, lang)

    return await target.reply(html.quote(text))


async def process_punto(_message: Message, meta: MetaInfo) -> Message | bool | None:
    ru_tab = "ЁёАБВГДЕЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯабвгдежзийклмнопрстуфхцчшщъыьэюя"
    en_tab = "~`F<DULT:PBQRKVYJGHCNEA{WXIO}SM\">Zf,dult;pbqrkvyjghcnea[wxio]sm'.z"
    ru_en = str.maketrans(ru_tab, en_tab)
    en_ru = str.maketrans(en_tab, ru_tab)

    target, text = meta.extract_text()
    if not text:
        return True

    lang = transliterate.detect_language(text, heavy_check=True)
    if lang not in ("en", "ru"):
        lang = "en"

    if lang == "ru":
        return await target.reply(html.quote(text.translate(ru_en)))

    text = text.translate(en_ru)
    return await target.reply(html.quote(text))


async def process_id(message: Message) -> Message | bool | None:
    text = ""

    for target in (message.reply_to_message, message):
        if target:
            origin = target.forward_origin
            forwarded: list[User | Chat | str | None] = []
            if isinstance(origin, MessageOriginUser):
                forwarded.append(origin.sender_user)
            elif isinstance(origin, MessageOriginChat):
                forwarded.extend((origin.sender_chat, origin.author_signature))
            elif isinstance(origin, MessageOriginChannel):
                forwarded.extend((origin.chat, origin.author_signature))
            elif isinstance(origin, MessageOriginHiddenUser):
                forwarded.append(origin.sender_user_name)
            for t in (*forwarded, target.sender_chat, target.from_user, target.chat, target.via_bot):
                id_: str | int
                if isinstance(t, str):
                    name = t
                    id_ = "🤷🏻‍♂️"
                    url = None
                elif isinstance(t, User):
                    name = t.full_name
                    id_ = t.id
                    url = t.url
                elif isinstance(t, Chat):
                    name = t.full_name
                    id_ = t.id
                    url = await chat_url(t)
                else:
                    continue

                link = f", ({hlink('🔗', url)})" if url else ""
                text += f"{hbold(name)}{link}:\n└ {hcode(id_)}\n\n"

    return await message.reply(text, disable_notification=True, disable_web_page_preview=True)


async def process_md(_message: Message, meta: MetaInfo) -> Message | bool | None:
    target, text = meta.extract_text()
    if not text:
        return True
    return await target.reply(hpre(target.md_text))


async def process_html(_message: Message, meta: MetaInfo) -> Message | bool | None:
    target, text = meta.extract_text()
    if not text:
        return True
    return await target.reply(hpre(target.html_text))


async def process_file_id(message: Message, meta: MetaInfo) -> None:
    t, *file_ids = re.split(r"\s", meta.text)

    if t == "photo":
        for file_id in file_ids:
            await message.reply_photo(file_id)

    elif t == "audio":
        for file_id in file_ids:
            await message.reply_audio(file_id)

    elif t == "document":
        for file_id in file_ids:
            await message.reply_document(file_id)

    elif t == "video":
        for file_id in file_ids:
            await message.reply_video(file_id)

    elif t == "animation":
        for file_id in file_ids:
            await message.reply_animation(file_id)

    elif t == "sticker":
        for file_id in file_ids:
            await message.reply_sticker(file_id)

    elif t == "video_note":
        for file_id in file_ids:
            await message.reply_video_note(file_id)

    elif t == "voice":
        for file_id in file_ids:
            await message.reply_voice(file_id)

    else:
        await message.reply_photo(t)
        for file_id in file_ids:
            await message.reply_photo(file_id)
