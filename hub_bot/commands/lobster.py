import io
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image
from aiogram.enums import ChatAction, ChatType
from aiogram.types import Message

from common.executor import TPExecutor
from common.tg.chat_actioner import ChatActioner
from common.tg.filters import MetaInfo
from common.tg.files import download, input_file
from common.tg.keyboards import rate_keyboard
from common.utils import image_bytes_io, megabytes
from hub_bot.utils.caption_layout import (
    CaptionLayoutError,
    demotivator_caption,
    demotivator_image,
    frame_border,
    lobster_image,
    MAX_IMAGE_SIDE,
)
from hub_bot.utils.ffmpeg import ffmpeg


async def process_lobster(message: Message, meta: MetaInfo, cpu_executor: TPExecutor) -> Message | bool:
    target, file = await meta.extract_image_with_downloading(with_profile_photo=True)
    if file is None:
        return True

    _, text = meta.extract_text()
    if not text:
        return True

    async with ChatActioner(message, ChatAction.UPLOAD_PHOTO):
        try:
            image, timeouted = await cpu_executor.run(lobster_image, file, text)
        except CaptionLayoutError as exc:
            return await message.reply(str(exc))
        if timeouted:
            return await message.reply("🤷🏻‍♂️ Timeout")

    return await target.reply_photo(
        input_file(image_bytes_io(image, ext="png"), "image.png"),
        reply_markup=rate_keyboard() if message.chat.type != ChatType.PRIVATE else None,
    )


async def process_demotivator(message: Message, meta: MetaInfo, cpu_executor: TPExecutor) -> Message | bool:
    _, video = await meta.extract_video()
    if video:
        return await process_demotivator_video(message, meta, cpu_executor)

    target, file = await meta.extract_image_with_downloading(with_profile_photo=True)
    if file is None:
        return True

    _, text = meta.extract_text()
    if not text:
        return True

    async with ChatActioner(message, ChatAction.UPLOAD_PHOTO):
        try:
            image, timeouted = await cpu_executor.run(demotivator_image, file, text)
        except CaptionLayoutError as exc:
            return await message.reply(str(exc))
        if timeouted:
            return await message.reply("🤷🏻‍♂️ Timeout")

    return await target.reply_photo(
        input_file(image_bytes_io(image, ext="png"), "image.png"),
        reply_markup=rate_keyboard() if message.chat.type != ChatType.PRIVATE else None,
    )


def demotivator_video(file: io.BytesIO, width: int, text: str) -> io.BytesIO | None:
    width = max(320, min(MAX_IMAGE_SIDE, int(width or 384)))
    width -= width % 2
    border = frame_border(width)
    frame_width = width + 12 + border * 2
    caption = demotivator_caption(frame_width, text, border)
    with TemporaryDirectory(prefix="hub-caption-") as directory:
        path = Path(directory) / "caption.png"
        caption.save(path)
        filters = (
            f"[0:v]scale=w='max(2,trunc(min({width},{MAX_IMAGE_SIDE}*dar)/2)*2)':"
            f"h='max(2,trunc(min({MAX_IMAGE_SIDE},{width}/dar)/2)*2)',setsar=1,"
            "pad=iw+6:ih+6:3:3:black,pad=iw+6:ih+6:3:3:white,"
            f"pad={frame_width}:ih+{border * 2 + caption.height}:(ow-iw)/2:{border}:black[frame];"
            "[frame][1:v]overlay=0:H-h:format=auto[out]"
        )
        parameters = [
            "-i",
            str(path),
            "-filter_complex",
            filters,
            "-map",
            "[out]",
            "-map",
            "0:a?",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            "-f",
            "mp4",
        ]
        return ffmpeg(file, parameters=parameters, out_suffix=".mp4")


async def process_demotivator_video(message: Message, meta: MetaInfo, cpu_executor: TPExecutor) -> Message | bool:
    target, file = await meta.extract_video()
    if file is None:
        return True
    if (getattr(file, "file_size", None) or 0) > megabytes(20):
        return await message.reply("🤷🏻‍♂️ Файл больше 20 Мб, не смогу скачать")
    io_bytes = await download(file)
    if io_bytes is None:
        return await message.reply("🤷🏻‍♂️ Что-то пошло не так")

    _, text = meta.extract_text()
    if not text:
        return True

    try:
        video, timeouted = await cpu_executor.run(demotivator_video, io_bytes, getattr(file, "width", 384), text)
    except CaptionLayoutError as exc:
        return await message.reply(str(exc))
    if timeouted:
        return await message.reply("🤷🏻‍♂️ Timeout")
    if video is None:
        return await message.reply("🤷🏻‍♂️ Что-то пошло не так")

    return await target.reply_video(
        input_file(video, "demotivator.mp4"), reply_markup=rate_keyboard() if message.chat.type != ChatType.PRIVATE else None
    )


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
        im2 = im1.transpose(Image.FLIP_LEFT_RIGHT)
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
        im2 = im1.transpose(Image.FLIP_TOP_BOTTOM)
        dst = Image.new("RGBA", (image.width, crop_size * 2))
        dst.paste(im1, (0, 0))
        dst.paste(im2, (0, im1.height))

    return await target.reply_photo(
        input_file(image_bytes_io(dst, ext="png"), "image.png"),
        reply_markup=rate_keyboard() if message.chat.type != ChatType.PRIVATE else None,
    )
