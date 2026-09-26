"""Mirrored-image novelty commands."""

from PIL import Image
from aiogram.enums import ChatAction, ChatType
from aiogram.types import Message

from msu_hub_bot.telegram.chat_actioner import ChatActioner
from msu_hub_bot.telegram.filters import MetaInfo
from msu_hub_bot.telegram.files import input_file
from msu_hub_bot.telegram.keyboards import rate_keyboard
from msu_hub_bot.utils import image_bytes_io


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
