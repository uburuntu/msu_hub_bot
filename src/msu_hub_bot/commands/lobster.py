"""Image and video captions with shared media selection and delivery."""

import io
from collections.abc import Callable
from contextlib import closing

from PIL import Image
from aiogram.enums import ChatAction, ChatType
from aiogram.types import Animation, Document, Message, Sticker, Video, VideoNote

from msu_hub_bot.execution.executor import TPExecutor
from msu_hub_bot.media.caption_layout import CaptionLayoutError, CaptionStyle, caption_image
from msu_hub_bot.media.caption_video import caption_video
from msu_hub_bot.telegram.chat_actioner import ChatActioner
from msu_hub_bot.telegram.command_api import MediaInput, MetaCommand, TextInput
from msu_hub_bot.telegram.filters import MetaInfo
from msu_hub_bot.telegram.files import DownloadableMedia, input_file
from msu_hub_bot.telegram.keyboards import rate_keyboard
from msu_hub_bot.telegram.media_jobs import DownloadUnavailable, run_downloaded
from msu_hub_bot.utils import image_bytes_io


async def _process_caption(text: str, media: DownloadableMedia, meta: MetaInfo, cpu_executor: TPExecutor, style: CaptionStyle) -> Message:
    message = meta.message
    is_video = (
        isinstance(media, (Animation, Video, VideoNote))
        or isinstance(media, Sticker)
        and media.is_video
        or isinstance(media, Document)
        and (media.mime_type or "").startswith("video/")
    )

    renderer: Callable[[io.BytesIO, str, CaptionStyle], Image.Image | io.BytesIO | None]
    renderer = caption_video if is_video else caption_image
    action = ChatAction.UPLOAD_VIDEO if is_video else ChatAction.UPLOAD_PHOTO
    async with ChatActioner(message, action):
        try:
            result, timeouted = await run_downloaded(cpu_executor, media, renderer, text, style, bot=message.bot)
        except DownloadUnavailable:
            return await message.reply("🤷🏻‍♂️ Не удалось скачать файл. Попробуй прислать его ещё раз.")
        except CaptionLayoutError as exc:
            return await message.reply(str(exc))
        if timeouted:
            if result is not None:
                result.close()
            return await message.reply("🤷🏻‍♂️ Обработка заняла слишком много времени. Попробуй файл поменьше.")
        if result is None:
            return await message.reply("🤷🏻‍♂️ Не удалось обработать видео" if is_video else "🤷🏻‍♂️ Не удалось обработать картинку")

        keyboard = rate_keyboard() if message.chat.type != ChatType.PRIVATE else None
        if isinstance(result, io.BytesIO):
            with result:
                video = input_file(result, f"{style}.mp4")
            return await meta.reply(video=video, reply_markup=keyboard, supports_streaming=True, fixed=True)
        with closing(result), image_bytes_io(result, ext="png") as output:
            photo = input_file(output, f"{style}.png")
        return await meta.reply(photo=photo, reply_markup=keyboard, fixed=True)


@MetaCommand(
    "lobster",
    "l",
    "л",
    "лобстер",
    text=TextInput(reply=True),
    media=MediaInput(kinds=("image", "video"), reply=True, avatar=True),
    max_output_bytes=50 * 1024 * 1024,
)
async def process_lobster(text: str, media: DownloadableMedia, meta: MetaInfo, cpu_executor: TPExecutor) -> Message:
    return await _process_caption(text, media, meta, cpu_executor, "lobster")


@MetaCommand(
    "demotivator",
    "de",
    "д",
    "де",
    text=TextInput(reply=True),
    media=MediaInput(kinds=("image", "video"), reply=True, avatar=True),
    max_output_bytes=50 * 1024 * 1024,
)
async def process_demotivator(text: str, media: DownloadableMedia, meta: MetaInfo, cpu_executor: TPExecutor) -> Message:
    return await _process_caption(text, media, meta, cpu_executor, "demotivator")


@MetaCommand(
    "meme",
    text=TextInput(reply=True),
    media=MediaInput(kinds=("image", "video"), reply=True, avatar=True),
    max_output_bytes=50 * 1024 * 1024,
)
async def process_meme(text: str, media: DownloadableMedia, meta: MetaInfo, cpu_executor: TPExecutor) -> Message:
    return await _process_caption(text, media, meta, cpu_executor, "meme")


async def process_atmta(message: Message, meta: MetaInfo) -> Message | bool:
    target, file = await meta.extract_image_with_downloading(with_profile_photo=True)
    if file is None:
        return True

    percent = 0.5
    if meta.arguments:
        try:
            percent = min(1.0, max(0.0, float(meta.arguments[0])))
        except ValueError:
            pass
    if percent <= 0:
        return await message.reply("Укажи долю больше 0 и не больше 1.")

    async with ChatActioner(message, ChatAction.UPLOAD_PHOTO):
        image = Image.open(file).convert("RGBA")
        crop_size = max(1, int(image.width * percent))

        im1 = image.crop((0, 0, crop_size, image.height))
        im2 = im1.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
        dst = Image.new("RGBA", (crop_size * 2, image.height))
        dst.paste(im1, (0, 0))
        dst.paste(im2, (im1.width, 0))

    return await target.reply_photo(
        input_file(image_bytes_io(dst, ext="png"), "image.png"),
        reply_markup=rate_keyboard() if message.chat.type != ChatType.PRIVATE else None,
    )


async def process_atmta_v(message: Message, meta: MetaInfo) -> Message | bool:
    target, file = await meta.extract_image_with_downloading(with_profile_photo=True)
    if file is None:
        return True

    percent = 0.5
    if meta.arguments:
        try:
            percent = min(1.0, max(0.0, float(meta.arguments[0])))
        except ValueError:
            pass
    if percent <= 0:
        return await message.reply("Укажи долю больше 0 и не больше 1.")

    async with ChatActioner(message, ChatAction.UPLOAD_PHOTO):
        image = Image.open(file).convert("RGBA")
        crop_size = max(1, int(image.height * percent))

        im1 = image.crop((0, 0, image.width, crop_size))
        im2 = im1.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
        dst = Image.new("RGBA", (image.width, crop_size * 2))
        dst.paste(im1, (0, 0))
        dst.paste(im2, (0, im1.height))

    return await target.reply_photo(
        input_file(image_bytes_io(dst, ext="png"), "image.png"),
        reply_markup=rate_keyboard() if message.chat.type != ChatType.PRIVATE else None,
    )
