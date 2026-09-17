from aiogram.types import Message
from aiogram import html
from aiogram.utils.markdown import hcode, hbold
from aiohttp import ClientError

from msu_hub_bot.providers.exceptions import ExternalServiceError
from msu_hub_bot.providers.lingvanex import languages_list, translate, translate_image
from msu_hub_bot.telegram.filters import MetaInfo


async def _reply_text(target: Message, text: str) -> Message:
    parts: list[str] = []
    size = 0
    for character in text:
        escaped = html.quote(character)
        width = max(len(escaped), len(character.encode("utf-16-le")) // 2)
        if size + width > 4000:
            await target.reply("".join(parts))
            parts = []
            size = 0
        parts.append(escaped)
        size += width
    return await target.reply("".join(parts))


async def tr(meta: MetaInfo, src: str, dest: str) -> Message | None:
    translated = ""
    failed = []

    target, file = await meta.extract_image_with_downloading()
    if file is not None:
        try:
            result = await translate_image(file, src, dest)
            if result and result.strip():
                translated += result + "\n\n"
            else:
                failed.append("изображение")
        except (ExternalServiceError, ClientError, TimeoutError):
            failed.append("изображение")

    target, text = meta.extract_text()
    if text:
        try:
            result = await translate(text, src, dest)
            if result and result.strip():
                translated += result
            else:
                failed.append("текст")
        except (ExternalServiceError, ClientError, TimeoutError):
            failed.append("текст")

    if translated:
        if failed:
            translated = translated.rstrip() + "\n\nНе удалось перевести " + " и ".join(failed) + "."
        return await _reply_text(target, translated)
    if failed:
        return await target.reply("Не удалось выполнить перевод. Попробуйте ещё раз позже.")
    return None


async def process_langs(message: Message) -> Message:
    langs = await languages_list()
    text = hbold("Поддерживаемые языки") + "\n\n"
    for lang in langs:
        code = html.quote(str(lang.get("code_alpha_1") or "")[:30])
        full_code = html.quote(str(lang.get("full_code") or "")[:30])
        name = html.quote(str(lang.get("englishName") or "")[:100])
        line = f"• {name} — {code}, {full_code}\n"
        if len(text.encode("utf-16-le")) // 2 + len(line.encode("utf-16-le")) // 2 > 4000:
            await message.reply(text)
            text = ""
        text += line

    usage = "\nИспользование: " + hcode("/tr en ru") + " — перевод с английского на русский"
    if len(text.encode("utf-16-le")) // 2 + len(usage) > 4000:
        await message.reply(text)
        text = ""
    text += usage
    return await message.reply(text)


async def process_en(_message: Message, meta: MetaInfo) -> Message | None:
    return await tr(meta, "ru", "en_GB")


async def process_ru(_message: Message, meta: MetaInfo) -> Message | None:
    return await tr(meta, "en_GB", "ru")


async def process_translate(message: Message, meta: MetaInfo) -> Message | None:
    args = meta.arguments
    if len(args) != 2:
        return await message.reply(
            "Использование: " + hcode("/tr en ru") + " — перевод с английского на русский, полный список языков: /langs"
        )
    src, dest = args[0], args[1]
    return await tr(meta, src, dest)
