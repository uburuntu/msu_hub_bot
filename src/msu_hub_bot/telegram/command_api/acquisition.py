"""Acquire declared inputs once and keep owned resources alive through delivery."""

import asyncio
import io
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated, get_args, get_origin

from PIL import Image, UnidentifiedImageError
from aiogram.types import Animation, Document, Message, PhotoSize, Sticker, Video, VideoNote

from msu_hub_bot.media.limits import MediaDimensionsError, validate_dimensions
from msu_hub_bot.telegram.context import bot_for
from msu_hub_bot.telegram.extraction import Extractor, SimpleExtractor
from msu_hub_bot.telegram.files import DownloadableMedia, DownloadTooLarge, download
from msu_hub_bot.telegram.filters import MetaInfo
from msu_hub_bot.telegram.rich_input import rich_text

from .inputs import DocumentInput, ImageInput, InputError, MediaDeclaration, MediaInput, TextInput, VideoInput


async def acquire_text(meta: MetaInfo, declaration: TextInput, resources: ExitStack) -> tuple[Message, str]:
    if meta.text:
        return meta.message, check_text(meta.text, declaration)
    targets = Extractor.targets(
        meta.message, Extractor.ReplyPolicy.prefer_origin if declaration.reply else Extractor.ReplyPolicy.only_origin
    )
    for target in targets:
        if target is not meta.message:
            text = target.text or target.caption or rich_text(target)
            if text:
                return target, check_text(text, declaration)
        if declaration.document:
            document = await SimpleExtractor.document(target)
            if document is not None and (document.mime_type or "").startswith("text/"):
                stream = await bounded_download(document, meta, declaration.max_bytes, resources)
                try:
                    text = stream.getvalue().decode("utf-8")
                except UnicodeDecodeError:
                    raise InputError("Пришли текстовый файл в UTF-8.") from None
                return target, check_text(text, declaration)
    return meta.message, ""


def check_text(text: str, declaration: TextInput) -> str:
    if declaration.max_chars is not None and len(text) > declaration.max_chars:
        raise InputError(f"Текст слишком длинный: максимум {declaration.max_chars} символов.")
    if len(text.encode("utf-8")) > declaration.max_bytes:
        raise InputError("Текст слишком большой. Пришли файл поменьше.")
    return text


async def select_media(meta: MetaInfo, declaration: MediaDeclaration) -> tuple[Message, DownloadableMedia | None]:
    targets = Extractor.targets(
        meta.message, Extractor.ReplyPolicy.prefer_origin if declaration.reply else Extractor.ReplyPolicy.only_origin
    )
    for target in targets:
        if isinstance(declaration, VideoInput) or isinstance(declaration, MediaInput) and "video" in declaration.kinds:
            video = await SimpleExtractor.video(target)
            if video is not None:
                return target, video
            if target.document is not None and (target.document.mime_type or "").startswith("video/"):
                return target, target.document
        if isinstance(declaration, ImageInput) or isinstance(declaration, MediaInput) and "image" in declaration.kinds:
            image = await SimpleExtractor.image(target)
            if image is not None:
                return target, image
        if isinstance(declaration, DocumentInput) or isinstance(declaration, MediaInput) and "document" in declaration.kinds:
            document = await SimpleExtractor.document(target)
            if document is not None:
                return target, document
        if isinstance(declaration, MediaInput) and "audio" in declaration.kinds and (sound := target.audio or target.voice):
            return target, sound
    if isinstance(declaration, (ImageInput, MediaInput)) and declaration.avatar:
        for target in reversed(targets):
            image = await SimpleExtractor.profile_photo(target)
            if image is not None:
                return target, image
    return meta.message, None


def cache_media(meta: MetaInfo, declaration: MediaDeclaration, source: Message, media: DownloadableMedia | None) -> None:
    if isinstance(declaration, ImageInput):
        meta._image_input = (source, media if isinstance(media, (PhotoSize, Document, Sticker)) else None)
    elif isinstance(declaration, VideoInput):
        meta._video_input = (source, media if isinstance(media, (Video, Animation, VideoNote, Sticker)) else None)
    elif isinstance(declaration, DocumentInput):
        meta._document_input = (source, media if isinstance(media, Document) else None)
    elif isinstance(media, (Video, Animation, VideoNote)) or isinstance(media, Sticker) and media.is_video:
        meta._video_input = (source, media)
    elif isinstance(media, (PhotoSize, Sticker)) or isinstance(media, Document) and (media.mime_type or "").startswith("image/"):
        meta._image_input = (source, media)
    elif isinstance(media, Document):
        meta._document_input = (source, media)


def representation(annotation: object) -> object:
    if get_origin(annotation) is Annotated:
        return representation(get_args(annotation)[0])
    args = get_args(annotation)
    if args:
        choices = [arg for arg in args if arg is not type(None)]
        if len(choices) == 1:
            return representation(choices[0])
    return annotation


async def acquire_media(meta: MetaInfo, declaration: MediaDeclaration, annotation: object, resources: ExitStack) -> tuple[Message, object]:
    source, media = await select_media(meta, declaration)
    cache_media(meta, declaration, source, media)
    if media is None:
        return source, None
    meta._input_limits[media.file_id] = declaration.max_bytes
    requested = representation(annotation)
    # Metadata consumers own their branch-specific admission, download and failure policy.
    if requested not in (bytes, io.BytesIO, Path, Image.Image):
        return source, media
    stream = await bounded_download(media, meta, declaration.max_bytes, resources)
    if requested is bytes:
        return source, stream.getvalue()
    if requested is io.BytesIO:
        return source, stream
    if requested is Path:
        directory = Path(resources.enter_context(TemporaryDirectory(prefix="hub-command-")))
        suffix = Path(getattr(media, "file_name", None) or "input").suffix
        suffix = suffix if len(suffix) <= 16 and suffix.removeprefix(".").isalnum() else ""
        path = directory / ("input" + suffix)
        path.write_bytes(stream.getvalue())
        return source, path

    payload = stream.getvalue()

    def decode() -> Image.Image:
        # The worker owns an independent stream even if event-loop shutdown cancels its Task.
        with io.BytesIO(payload) as owned:
            image = Image.open(owned)
            try:
                validate_dimensions(*image.size)
                image.load()
            except BaseException:
                image.close()
                raise
            return image

    task = asyncio.create_task(asyncio.to_thread(decode))
    try:
        image = await asyncio.shield(task)
    except asyncio.CancelledError:
        # A second cancellation must not abandon the worker's returned image.
        while True:
            try:
                image = await asyncio.shield(task)
            except asyncio.CancelledError:
                if task.cancelled():
                    break
            except Exception:
                break
            else:
                image.close()
                break
        raise
    except UnidentifiedImageError, OSError, Image.DecompressionBombError, MediaDimensionsError:
        raise InputError("Не удалось открыть изображение. Пришли картинку поменьше.") from None
    resources.callback(image.close)
    return source, image


async def bounded_download(media: DownloadableMedia, meta: MetaInfo, limit: int, resources: ExitStack) -> io.BytesIO:
    if cached := meta._downloads.get(media.file_id):
        if cached.closed:
            raise RuntimeError("Command input was closed before its invocation completed")
        if cached.getbuffer().nbytes > limit:
            raise InputError("Файл слишком большой. Пришли файл поменьше.")
        cached.seek(0)
        return cached
    if (media.file_size or 0) > limit:
        raise InputError("Файл слишком большой. Пришли файл поменьше.")
    try:
        stream = await download(media, bot_for(meta.message), max_bytes=limit)
    except DownloadTooLarge:
        raise InputError("Файл слишком большой. Пришли файл поменьше.") from None
    if stream is None:
        raise InputError("Не удалось скачать файл. Попробуй прислать его ещё раз.")
    resources.callback(stream.close)
    meta._downloads[media.file_id] = stream
    return stream
