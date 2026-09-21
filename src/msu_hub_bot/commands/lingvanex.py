import asyncio
import io
from contextlib import nullcontext

from aiogram.types import Message
from aiogram import html
from aiogram.exceptions import TelegramAPIError
from aiogram.utils.markdown import hcode, hbold
from aiohttp import ClientError

from msu_hub_bot.media.limits import MAX_DOWNLOAD_BYTES
from msu_hub_bot.providers.exceptions import ExternalServiceError
from msu_hub_bot.providers.jev import MAX_LANGUAGE_TEXT, JevClient, JevError, JevErrorReason
from msu_hub_bot.providers.language_resolution import LanguageCatalogue, LanguageResolutionError, TranslationBody, translation_body
from msu_hub_bot.providers.lingvanex import languages_list, translate, translate_image
from msu_hub_bot.settings import MissingIntegration
from msu_hub_bot.telegram.command_api import ImageInput, InputError, MetaCommand, MetaInfo, TextInput
from msu_hub_bot.telegram.extraction import ImageMedia, SimpleExtractor
from msu_hub_bot.telegram.files import DownloadTooLarge, download
from msu_hub_bot.telegram.recent_context import RecentMessages
from msu_hub_bot.telegram.rich_input import rich_text
from msu_hub_bot.telemetry import Boundary, Outcome, Provider, Telemetry

TRANSLATE_GUIDANCE = "Укажи языки из /langs: /tr en ru. Добавь текст после кодов или ответь на нужное сообщение."


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


async def _translate_parts(file: io.BytesIO | None, text: str, src: str, dest: str, *, image_failed: bool = False) -> str | None:
    translated = ""
    failed = ["изображение"] if image_failed else []

    if file is not None:
        try:
            with file:
                result = await translate_image(file, src, dest)
            if result and result.strip():
                translated += result + "\n\n"
            else:
                failed.append("изображение")
        except ExternalServiceError, ClientError, TimeoutError:
            failed.append("изображение")

    if text:
        try:
            result = await translate(text, src, dest)
            if result and result.strip():
                translated += result
            else:
                failed.append("текст")
        except ExternalServiceError, ClientError, TimeoutError:
            failed.append("текст")

    if translated:
        if failed:
            translated = translated.rstrip() + "\n\nНе удалось перевести " + " и ".join(failed) + "."
        return translated
    if failed:
        return "Не удалось выполнить перевод. Попробуйте ещё раз позже."
    return None


async def tr(meta: MetaInfo, src: str, dest: str) -> Message | None:
    _, file = await meta.extract_image_with_downloading()
    target, text = meta.extract_text()
    translated = await _translate_parts(file, text, src, dest)
    return await _reply_text(target, translated) if translated else None


async def process_langs(message: Message) -> Message:
    langs: object = await languages_list()
    if not isinstance(langs, list) or any(not isinstance(lang, dict) for lang in langs):
        raise LanguageResolutionError()
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


async def resolve_languages(
    *,
    meta: MetaInfo,
    source: str | None,
    target: str | None,
    text: str | None = None,
    jev: JevClient | None = None,
    recent_messages: RecentMessages | None = None,
    telemetry: Telemetry | None = None,
) -> dict[str, object]:
    """Normalize explicit arguments; ask Jev only about remaining languages."""
    try:
        async with asyncio.timeout(10):
            catalogue = LanguageCatalogue.from_provider(await languages_list())
    except ExternalServiceError, ClientError, TimeoutError, MissingIntegration, ValueError:
        raise InputError(TRANSLATE_GUIDANCE) from None
    source, target = catalogue.normalize(source), catalogue.normalize(target)
    if source is not None and target is not None:
        return {"source": source, "target": target}

    reply = meta.message.reply_to_message
    reply_text = reply.text or reply.caption or rich_text(reply) if reply is not None else None
    body: TranslationBody | None
    if meta.hashtag:
        # Hashtag arguments are separate from the surrounding body already.
        selected, selected_text = meta.extract_text()
        body = TranslationBody(selected_text, " ".join(meta.arguments), selected is reply)
    else:
        body = translation_body(meta.raw_text, reply_text, known_source=source is not None, known_target=target is not None)
    if body is None or not body.text:
        # An image-only command can name its source explicitly in prose. The
        # language judge receives no pixels and must not infer an image language.
        image = await SimpleExtractor.image(meta.message)
        if image is None and reply is not None:
            image = await SimpleExtractor.image(reply)
        if image is None:
            raise InputError(TRANSLATE_GUIDANCE)
        if body is None:
            body = TranslationBody("", meta.raw_text, reply is not None)
    if jev is None:
        raise InputError(TRANSLATE_GUIDANCE)
    context = recent_messages.before(meta.message, meta.context_messages) if recent_messages is not None else ()
    with (
        telemetry.operation(Boundary.PROVIDER, "jev.resolve_languages", provider=Provider.JEV)
        if telemetry is not None
        else nullcontext(None)
    ) as observation:
        try:
            result = await jev.resolve_languages(
                body.request,
                catalogue.choices,
                text=body.text[:MAX_LANGUAGE_TEXT],
                source=source,
                target=target,
                recent_messages=context,
            )
        except JevError as error:
            if observation is not None:
                observation.set_outcome(Outcome.TIMEOUT if error.reason is JevErrorReason.TIMEOUT else Outcome.UNAVAILABLE)
            raise InputError(TRANSLATE_GUIDANCE) from None
        if observation is not None:
            observation.model_usage(result.input_tokens, result.output_tokens, result.cost)
    if result.source not in catalogue.choices or result.target not in catalogue.choices:
        raise InputError(TRANSLATE_GUIDANCE)
    meta.input_sources["text"] = reply if body.from_reply and reply is not None else meta.message
    return {"source": source or result.source, "target": target or result.target, "text": body.text}


@MetaCommand(
    "translate",
    "tr",
    text=TextInput(reply=True, max_chars=32_768),
    image=ImageInput(reply=True),
    resolve=resolve_languages,
    context_messages=5,
    guidance=TRANSLATE_GUIDANCE,
)
async def process_translate(
    source: str, target: str, meta: MetaInfo, text: str = "", image: ImageMedia | None = None
) -> list[Message] | None:
    """Translate declared text and image inputs, preserving either successful part."""
    if not text and image is None:
        raise InputError(TRANSLATE_GUIDANCE)
    file = None
    image_failed = False
    if image is not None:
        try:
            file = await download(image, meta.message.bot, max_bytes=MAX_DOWNLOAD_BYTES)
        except ExternalServiceError, ClientError, TimeoutError, DownloadTooLarge, TelegramAPIError:
            image_failed = True
        if file is None:
            image_failed = True
    translated = await _translate_parts(file, text, source, target, image_failed=image_failed)
    if translated is None:
        return None
    # Translation historically chooses its text source after combining both
    # parts. A captioning command's image-first target policy does not apply.
    response_target = meta.extract_text()[0]
    if not text and image is not None:
        response_target = meta.input_sources.get("image", response_target)
    return await meta.reply(translated, to=response_target)
