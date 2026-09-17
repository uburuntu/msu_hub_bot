import io
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Optional

import cv2
from cachetools import TTLCache
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, InputMediaPhoto
from aiogram.filters.callback_data import CallbackData

from msu_hub_bot.execution.executor import TPExecutor
from msu_hub_bot.telegram.callbacks import CallbackCommandBase
from msu_hub_bot.telegram.files import input_file
from msu_hub_bot.utils import bytes_io


def capture() -> cv2.VideoCapture:
    return cv2.VideoCapture("http://cam.mnc.ru/axis-cgi/mjpg/video.cgi?camera=1")


def camera(name: str) -> Optional[bytes]:
    if name == "msu":
        ret, frame = capture().read()

        if not ret:
            ret, frame = capture().read()

        if ret:
            t = Path(tempfile.gettempdir()) / Path(tempfile.mktemp(suffix=".jpg"))
            cv2.imwrite(str(t), frame)
            return t.read_bytes()

    return None


_camera_cache: TTLCache[str, bytes | None] = TTLCache(maxsize=1, ttl=10)


async def _camera_msu(cpu_executor: TPExecutor) -> Optional[bytes]:
    if "msu" in _camera_cache:
        return _camera_cache["msu"]
    content, timeouted = await cpu_executor.run(camera, "msu")
    result = content if not timeouted and isinstance(content, bytes) else None
    _camera_cache["msu"] = result
    return result


async def camera_msu(cpu_executor: TPExecutor) -> Optional[io.BytesIO]:
    content = await _camera_msu(cpu_executor)
    if not content:
        return None
    return bytes_io(content)


class CameraCallback(CallbackData, prefix="camera"):
    name: str
    action: str


class Camera(CallbackCommandBase):
    callback_data = CameraCallback

    @classmethod
    def keyboard(cls, name: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="🔄 Обновить", callback_data=CameraCallback(name=name, action="update").pack()),
                    InlineKeyboardButton(text="⏹ Сохранить вид", callback_data=CameraCallback(name=name, action="stop").pack()),
                ]
            ]
        )

    @classmethod
    async def process(cls, message: Message, cpu_executor: TPExecutor) -> Message:
        target = message.reply_to_message or message
        if photo := await camera_msu(cpu_executor):
            return await target.reply_photo(input_file(photo, "camera.jpg"), reply_markup=cls.keyboard("msu"))
        return await message.reply("😔 Камера недоступна")

    @classmethod
    async def process_cb(cls, query: CallbackQuery, callback_data: CameraCallback, cpu_executor: TPExecutor) -> Message | bool:
        m = query.message
        if not isinstance(m, Message):
            return await query.answer()

        name, action = callback_data.name, callback_data.action

        if action == "stop":
            await query.answer("✅ Вид сохранён", cache_time=5)
            return await m.edit_reply_markup(reply_markup=None)

        await query.answer("✅ Вид обновляется", cache_time=1)

        async with cls.lock(m.chat.id):
            if photo := await camera_msu(cpu_executor):
                with suppress(TelegramBadRequest):
                    return await m.edit_media(InputMediaPhoto(media=input_file(photo, "camera.jpg")), reply_markup=cls.keyboard(name))

        return True
