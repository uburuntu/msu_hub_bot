from dataclasses import replace

from aiogram.types import Message
from aiogram import html
from aiogram.utils.markdown import hcode, hbold
from aiohttp import ClientError

from msu_hub_bot.commands.language import LanguageDetector
from msu_hub_bot.execution.executor import TPExecutor
from msu_hub_bot.providers.exceptions import ExternalServiceError
from msu_hub_bot.providers.lingvanex import Translation, is_language, languages_list, same_language, translate, translate_image
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


USAGE = (
    "Использование: "
    + hcode("/tr")
    + " — на русский (русский текст — на английский), "
    + hcode("/tr de")
    + " — на немецкий, "
    + hcode("/tr en ru")
    + " — с английского на русский. Полный список языков: /langs"
)
ALREADY = {"ru": "Текст уже на русском.", "en": "Текст уже на английском."}


def default_target(source: str | None) -> str:
    return "en_GB" if same_language(source, "ru") else "ru"


def image_fallback(dest: str | None) -> str:
    # Without a confident detection, keep the earlier fixed guesses: /en read Russian, others English.
    return "ru" if same_language(dest, "en") else "en_GB"


async def tr(
    meta: MetaInfo,
    src: str | None,
    dest: str | None,
    language_detector: LanguageDetector | None = None,
    cpu_executor: TPExecutor | None = None,
) -> Message | None:
    """Translate the image and text; ``dest=None`` means Russian, or English for Russian sources."""
    translated = ""
    failed = []
    notices = []

    target, file = await meta.extract_image_with_downloading()
    if file is not None:
        try:
            image_src = src
            if image_src is None and language_detector is not None and cpu_executor is not None:
                image_src = await language_detector.detect_image(file.getvalue(), cpu_executor)
            image_src = image_src or image_fallback(dest)
            result = await translate_image(file, image_src, dest or default_target(image_src))
            if result and result.strip():
                translated += result + "\n\n"
            else:
                failed.append("изображение")
        except ExternalServiceError, ClientError, TimeoutError:
            failed.append("изображение")

    target, text = meta.extract_text()
    if text:
        try:
            translation: Translation | None = await translate(text, src, dest or "ru")
            if translation is not None and dest is None and same_language(translation.source, "ru"):
                translation = await translate(text, "ru", "en_GB")
            elif translation is not None and src is None and dest is not None and same_language(translation.source, dest):
                notices.append(ALREADY.get(dest[:2].lower(), "Текст уже на этом языке."))
                translation = None
            if translation is not None:
                if translation.text.strip():
                    translated += translation.text
                else:
                    failed.append("текст")
        except ExternalServiceError, ClientError, TimeoutError:
            failed.append("текст")

    if translated:
        extra = ([f"Не удалось перевести {' и '.join(failed)}."] if failed else []) + notices
        if extra:
            translated = translated.rstrip() + "\n\n" + "\n".join(extra)
        return await _reply_text(target, translated)
    if failed:
        return await target.reply("Не удалось выполнить перевод. Попробуйте ещё раз позже.")
    if notices:
        return await target.reply("\n".join(notices))
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

    usage = "\n" + USAGE
    if len(text.encode("utf-16-le")) // 2 + len(usage) > 4000:
        await message.reply(text)
        text = ""
    text += usage
    return await message.reply(text)


async def process_en(
    _message: Message, meta: MetaInfo, cpu_executor: TPExecutor, language_detector: LanguageDetector | None = None
) -> Message | None:
    return await tr(meta, None, "en_GB", language_detector, cpu_executor)


async def process_ru(
    _message: Message, meta: MetaInfo, cpu_executor: TPExecutor, language_detector: LanguageDetector | None = None
) -> Message | None:
    return await tr(meta, None, "ru", language_detector, cpu_executor)


async def process_translate(
    message: Message, meta: MetaInfo, cpu_executor: TPExecutor, language_detector: LanguageDetector | None = None
) -> Message | None:
    # Leading language codes are arguments: one is the target, two are source and target.
    # Other words were only captured positionally and belong to the text.
    codes: list[str] = []
    words: list[str] = []
    for argument in meta.arguments:
        if not words and len(codes) < 2 and await is_language(argument):
            codes.append(argument)
        else:
            words.append(argument)
    if words:
        meta = replace(meta, arguments=codes, text=" ".join([*words, meta.text]).strip())
    src, dest = (codes[0], codes[1]) if len(codes) == 2 else (None, codes[0] if codes else None)
    result = await tr(meta, src, dest, language_detector, cpu_executor)
    if result is None:
        return await message.reply(USAGE)
    return result
