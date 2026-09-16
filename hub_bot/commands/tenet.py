import io
from typing import Optional

from PIL import Image, ImageFile, ImageOps
from aiogram import Bot, html
from aiogram.types import Message, InputPollOption, InputSticker
from aiogram.utils.markdown import hcode

from common.executor import TPExecutor
from common.tg.files import download, input_file
from common.tg.utils import action_by_type
from common.utils import megabytes, image_bytes_io
from hub_bot.utils.ffmpeg import ffmpeg
from msu_hub_bot.settings import settings

ImageFile.LOAD_TRUNCATED_IMAGES = True


def reverse_audio(file: io.BytesIO) -> Optional[io.BytesIO]:
    parameters = [
        "-vf",
        "reverse",
        "-af",
        "areverse",
        "-f",
        "ogg",
        "-acodec",
        "libopus",
    ]
    result = ffmpeg(file, parameters=parameters, out_suffix=".ogg")
    return result


def reverse_video(file: io.BytesIO) -> Optional[io.BytesIO]:
    parameters = [
        "-vf",
        "reverse",
        "-af",
        "areverse",
        "-f",
        "mp4",
    ]
    result = ffmpeg(file, parameters=parameters, out_suffix=".mp4")
    return result


def reverse_webm(file: io.BytesIO) -> Optional[io.BytesIO]:
    parameters = [
        "-vf",
        "reverse",
        "-c:v",
        "libvpx-vp9",
    ]
    result = ffmpeg(file, parameters=parameters, out_suffix=".webm")
    return result


def mirror_image(file: io.BytesIO, name: str = "", ext: str = "png") -> io.BytesIO:
    image = ImageOps.mirror(Image.open(file))
    return image_bytes_io(image, name or "image", ext)


async def process_reverse(message: Message, bot: Bot, cpu_executor: TPExecutor) -> Message | bool:
    target = message.reply_to_message
    if not target:
        if message.from_user:
            photos = (await bot.get_user_profile_photos(user_id=message.from_user.id, limit=1)).photos
            if photos:
                file = await download(photos[0][-1], bot)
                if file is not None:
                    return await message.reply_photo(input_file(mirror_image(file), "image.png"))
        return True

    if action := action_by_type(target.content_type):
        await bot.send_chat_action(chat_id=target.chat.id, action=action, message_thread_id=target.message_thread_id)
    if text := target.text:
        return await target.reply(html.quote(text[::-1]))

    if sticker := target.sticker:
        if sticker.is_animated:
            return True
        file = await download(sticker, bot)
        if file is None:
            return True
        if sticker.is_video:
            result_file, timeouted = await cpu_executor.run(reverse_webm, file)
            if timeouted:
                return await message.reply(hcode("🤷🏻‍♂️ Timeout"))
            if not result_file:
                return await message.reply(hcode("🤷🏻‍♂️ Не удалось выполнить запрос"))
            await bot.add_sticker_to_set(
                user_id=settings.tenet_sticker_owner_id,
                name="tenet_webm_by_msu_hub_bot",
                sticker=InputSticker(sticker=input_file(result_file, "sticker.webm"), format="video", emoji_list=["🔄"]),
            )
            pack = await bot.get_sticker_set("tenet_webm_by_msu_hub_bot")
            await target.reply_sticker(pack.stickers[-1].file_id)
            await bot.delete_sticker_from_set(sticker=pack.stickers[-1].file_id)
            return True
        return await target.reply_sticker(input_file(mirror_image(file, ext="webp"), "sticker.webp"))

    if poll := target.poll:
        return await target.reply_poll(
            question=poll.question[::-1],
            options=[InputPollOption(text=option.text[::-1]) for option in poll.options],
            type=poll.type,
            allows_multiple_answers=poll.allows_multiple_answers,
            # The first answer is deliberately correct in a reversed quiz.
            correct_option_ids=[0] if poll.type == "quiz" else None,
            explanation=poll.explanation and poll.explanation[::-1],
        )

    if location := (target.venue.location if target.venue else target.location):
        lat, lon = location.latitude, location.longitude
        return await message.reply_location(latitude=-lat, longitude=lon - 180 if lon > 0 else lon + 180)

    caption = html.quote(target.caption[::-1]) if target.caption else ""
    if target.photo:
        file = await download(target.photo[-1], bot)
        if file is None:
            return True
        return await target.reply_photo(input_file(mirror_image(file), "image.png"), caption=caption)

    document = target.document
    if document and document.mime_type in ("image/jpeg", "image/jpg", "image/png"):
        if (document.file_size or 0) > megabytes(20):
            return await message.reply(hcode("🤷🏻‍♂️ Мне недоступны файлы больше 20 Мб"))
        file = await download(document, bot)
        if file is None:
            return True
        name = (document.file_name or "image").rsplit(".", 1)[0][::-1]
        ext = "png" if document.mime_type == "image/png" else "jpeg"
        return await target.reply_document(input_file(mirror_image(file, name, ext), f"{name}.{ext}"), caption=caption)

    media = target.voice or target.audio or target.animation or target.video_note or target.video
    if media:
        if (media.file_size or 0) > megabytes(20):
            return await message.reply(hcode("🤷🏻‍♂️ Мне недоступны файлы больше 20 Мб"))
        file = await download(media, bot)
        if file is None:
            return True
        reverse = reverse_audio if target.voice or target.audio else reverse_video
        result_file, timeouted = await cpu_executor.run(reverse, file)
        if timeouted:
            return await message.reply(hcode("🤷🏻‍♂️ Timeout"))
        if not result_file:
            return await message.reply(hcode("🤷🏻‍♂️ Не удалось выполнить запрос, возможно файл слишком большой"))
        if target.voice or target.audio:
            return await target.reply_voice(input_file(result_file, "audio.ogg"), caption=caption, duration=media.duration)
        video_file = input_file(result_file, "video.mp4")
        if target.animation:
            return await target.reply_animation(video_file, caption=caption)
        if target.video_note:
            return await target.reply_video_note(video_file, duration=media.duration)
        return await target.reply_video(video_file, caption=caption, duration=media.duration)
    return True
