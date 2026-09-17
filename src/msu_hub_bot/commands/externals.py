from collections.abc import Awaitable, Callable
from io import BytesIO
from typing import Any
from urllib import parse

from aiogram import html

from aiogram.enums import ChatAction, ContentType
from aiogram.types import Message, InputFile, InputMediaPhoto, InputMediaVideo, URLInputFile, LinkPreviewOptions
from aiogram.utils.markdown import hitalic, hbold, hlink, hcode
from yarl import URL

from msu_hub_bot.telegram.constants import TELEGRAM_MESSAGE_MAX_LEN
from msu_hub_bot.providers.exceptions import ExternalServiceError
from msu_hub_bot.providers.moe import which_anime
from msu_hub_bot.providers.other import remove_bg, porfirevich, imgur_upload, duckduckgo
from msu_hub_bot.providers.topdf import convert_to_pdf
from msu_hub_bot.providers.urbandictionary import urban_dictionary
from msu_hub_bot.telegram.chat_actioner import ChatActioner
from msu_hub_bot.telegram.filters import MetaInfo
from msu_hub_bot.telegram.delivery import reply_album
from msu_hub_bot.telegram.files import input_file
from msu_hub_bot.telegram.utils import download, extract_image, action_by_type, send_super_reply
from msu_hub_bot.utils import one_liner, prettify_bytes, cut_long_text


def _escaped_excerpt(text: str, limit: int, *, tail: bool = False) -> str:
    parts, size = [], 0
    for character in reversed(text) if tail else text:
        escaped = html.quote(character)
        width = len(escaped.encode("utf-16-le")) // 2
        if size + width > max(0, limit - 1):
            break
        parts.append(escaped)
        size += width
    clipped = len(parts) < len(text)
    if tail:
        return ("…" if clipped else "") + "".join(reversed(parts))
    return "".join(parts) + ("…" if clipped else "")


def _upload(value: bytes | BytesIO | str | InputFile) -> str | InputFile:
    return value if isinstance(value, (str, InputFile)) else input_file(value)


async def process_external(
    message: Message,
    function: Callable[[BytesIO], Awaitable[Any]],
    output_type: str = ContentType.PHOTO,
    handler: Callable[[Any], Any] | None = None,
    async_handler: Callable[[Any], Awaitable[Any]] | None = None,
    error_text: str | None = None,
) -> Message | list[Message] | bool:
    target, dest = await extract_image(message, with_profile_photo=True)
    file = await download(dest)
    if file is None:
        return True

    try:
        async with ChatActioner(message, action_by_type(output_type) or ChatAction.TYPING):
            result = await function(file)
    except ExternalServiceError as e:
        return await message.reply(error_text or hitalic(f"🤷🏻‍♂️ {e.text}"))

    if handler is not None:
        result = handler(result)
    if async_handler is not None:
        result = await async_handler(result)

    if output_type == ContentType.TEXT:
        return await target.reply(result)
    if output_type == ContentType.DOCUMENT:
        return await target.reply_document(_upload(result))
    if output_type == ContentType.VIDEO:
        return await target.reply_video(_upload(result))
    if output_type == ContentType.ANIMATION:
        return await target.reply_animation(_upload(result))
    if output_type == "list[photo]":
        return await reply_album(target, [InputMediaPhoto(media=_upload(value)) for value in result])
    return await target.reply_photo(_upload(result))


async def process_which_anime(message: Message) -> Message | list[Message] | bool:
    target, dest = await extract_image(message, with_profile_photo=True)
    file = await download(dest)
    if file is None:
        return True

    async with ChatActioner(message, ChatAction.TYPING):
        try:
            result = await which_anime(file)
        except ExternalServiceError as e:
            return await message.reply(hitalic(f"🤷🏻‍♂️ {e.text}"))

        caption = ""
        files = []
        for anime in result["result"][:3]:
            link = hlink("Anilist", f"https://anilist.co/anime/{anime['anilist']}")
            filename = anime["filename"]
            if len(filename) > 100:
                filename = filename[:99] + "…"
            caption += f"— {hcode(filename)}, {link}, похожесть: {float(anime['similarity']):.2}\n\n"
            files.append(anime["video"])

        if not files:
            return await target.reply("Не удалось найти аниме по этому кадру. Попробуйте другой.")

        if len(files) == 1:
            url = files[0]
            return await target.reply_video(URLInputFile(url, filename=URL(url).name), caption=caption)

        media = []
        for url in files:
            media.append(InputMediaVideo(media=URLInputFile(url, filename=URL(url).name), caption=caption))
            caption = ""

        return await reply_album(target, media)


async def process_bg(message: Message) -> Message | list[Message] | bool:
    return await process_external(
        message,
        remove_bg,
        output_type=ContentType.DOCUMENT,
        error_text="Не удалось убрать фон. Попробуйте другое фото или повторите позже.",
    )


async def process_duckduckgo(message: Message, meta: MetaInfo) -> Message | bool | None:
    target, query = meta.extract_text()

    query = one_liner(cut_long_text(query, hard_max_len=100)[0]).strip().replace("\u200b", "")

    if not query:
        return True

    def lines(t: str) -> str:
        if t:
            return "\n" + t + "\n"
        return ""

    async with ChatActioner(message, ChatAction.TYPING):
        search_url = "https://duckduckgo.com/?" + parse.urlencode({"q": query})
        try:
            r = await duckduckgo(query)
        except ExternalServiceError:
            return await target.reply(
                "Поиск сейчас недоступен. Попробуйте " + hlink("DuckDuckGo", search_url),
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )

        if r["Redirect"]:
            return await target.reply(hlink(r["Redirect"], r["Redirect"]), link_preview_options=LinkPreviewOptions(is_disabled=True))

        heading = "<b>" + _escaped_excerpt(r["Heading"], 500) + "</b>"
        abstract = _escaped_excerpt(r["AbstractText"], 2500)
        source = _escaped_excerpt(parse.unquote(r["AbstractURL"]), 500)
        text = f"{heading}\n{lines(abstract)}\n{source}".strip()

        if not text or text == "<b></b>":
            return await message.reply(
                "🤷🏻‍♂️ Ничего не найдено\n\nИскать на " + hlink("DuckDuckGo", search_url),
                link_preview_options=LinkPreviewOptions(is_disabled=False),
            )

        preview = r["AbstractURL"]
        if not preview and r["Image"]:
            preview = "https://api.duckduckgo.com/" + r["Image"]

        return await send_super_reply(target, text=text, web_preview=preview)


async def process_imgur(message: Message) -> Message | list[Message] | bool:
    def handler(r: dict[str, Any]) -> str:
        return html.quote(r["link"]) + " | " + str(r["width"]) + "x" + str(r["height"]) + " | " + prettify_bytes(r["size"])

    return await process_external(
        message,
        imgur_upload,
        output_type=ContentType.TEXT,
        handler=handler,
        error_text="Не удалось загрузить файл на Imgur. Попробуйте позже.",
    )


async def process_ud(message: Message, meta: MetaInfo) -> Message | bool:
    target, text = meta.extract_text()
    if text is None:
        return True

    try:
        async with ChatActioner(message, ChatAction.TYPING):
            result = await urban_dictionary(text)
    except ExternalServiceError as e:
        return await message.reply(hitalic(f"🤷🏻‍♂️ {e.text}"))

    if not result:
        return await target.reply("В Urban Dictionary ничего не нашлось. Попробуйте другое слово.")

    texts: list[str] = []
    prev_header, total_len = None, 0
    for r in result[:3]:
        text = ""
        text += f"{hbold(r['header'])}\n\n" if r["header"].casefold() != prev_header else ""
        text += f"{html.quote(r['meaning'])}\n\n"
        text += f"Example:\n{hitalic(r['example'])}\n\n"
        text += f"👍🏻 {hbold(r['up'])} 👎🏻 {hbold(r['down'])}\n"
        text += f"{hbold('———')}\n"

        prev_header = r["header"].casefold()
        text_length = len(text.encode("utf-16-le")) // 2
        if total_len + text_length + bool(texts) > TELEGRAM_MESSAGE_MAX_LEN:
            if not texts:
                header = hbold(r["header"][:100]) + "\n\n"
                url = "https://www.urbandictionary.com/define.php?" + parse.urlencode({"term": r["header"][:100]})
                footer = "\n\n" + hlink("Полное определение", url)
                # Escaping expands one character to at most five; keep the excerpt and HTML intact.
                overhead = len((header + footer).encode("utf-16-le")) // 2
                limit = max(1, (TELEGRAM_MESSAGE_MAX_LEN - overhead - 1) // 5)
                texts.append(header + html.quote(r["meaning"][:limit]) + "…" + footer)
            break
        texts.append(text)
        total_len += text_length + (len(texts) > 1)

    return await target.reply("\n".join(texts))


async def process_topdf(message: Message, meta: MetaInfo) -> Message | bool:
    target, dest = await meta.extract_doc()
    if dest is None:
        return True

    try:
        async with ChatActioner(message, ChatAction.UPLOAD_DOCUMENT):
            file = await download(dest)
            if file is None:
                return await message.reply("Не удалось скачать файл. Попробуйте ещё раз.")
            url, thumb, convert_name = await convert_to_pdf(
                file, dest.file_name or "document", dest.mime_type or "application/octet-stream"
            )
    except TimeoutError:
        return await message.reply("Конвертация заняла слишком много времени. Попробуйте ещё раз позже.")
    except ExternalServiceError:
        return await message.reply("Не удалось преобразовать файл в PDF. Попробуйте позже.")

    return await target.reply_document(URLInputFile(url, filename=convert_name), thumbnail=URLInputFile(thumb))


async def process_porfirevich(message: Message, meta: MetaInfo) -> Message | bool:
    target, text = meta.extract_text()
    if not text:
        return True

    try:
        async with ChatActioner(message, ChatAction.TYPING):
            result = await porfirevich(text)
    except ExternalServiceError as e:
        return await message.reply(hitalic(f"🤷🏻‍♂️ {e.text}"))

    continuation = _escaped_excerpt(result, 4000)
    remaining = TELEGRAM_MESSAGE_MAX_LEN - len(continuation.encode("utf-16-le")) // 2 - len("<b></b>")
    result = "<b>" + _escaped_excerpt(text, remaining, tail=True) + "</b>" + continuation
    return await target.reply(result)
