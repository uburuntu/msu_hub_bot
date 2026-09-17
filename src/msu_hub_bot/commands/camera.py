import io
import logging
import subprocess
from contextlib import suppress
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Optional

from cachetools import TTLCache
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, InputMediaPhoto
from aiogram.filters.callback_data import CallbackData

from msu_hub_bot.execution.executor import TPExecutor
from msu_hub_bot.telegram.callbacks import CallbackCommandBase
from msu_hub_bot.telegram.files import input_file
from msu_hub_bot.utils import bytes_io


CAMERA_URL = "http://cam.mnc.ru/axis-cgi/mjpg/video.cgi?camera=1"
CAMERA_TIMEOUT = 10
CAMERA_MAX_BYTES = 4 * 1024 * 1024
logger = logging.getLogger(__name__)


def camera_frame(source: str) -> bytes | None:
    """Read one frame with native I/O, process and JPEG output limits."""
    try:
        with TemporaryDirectory(prefix="hub-camera-") as directory:
            output = Path(directory) / "frame.jpg"
            subprocess.run(
                [
                    "ffmpeg",
                    "-nostdin",
                    "-v",
                    "error",
                    "-y",
                    "-rw_timeout",
                    "5000000",
                    "-i",
                    source,
                    "-an",
                    "-sn",
                    "-dn",
                    "-frames:v",
                    "1",
                    "-q:v",
                    "2",
                    "-fs",
                    str(CAMERA_MAX_BYTES),
                    "-f",
                    "image2",
                    str(output),
                ],
                check=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=CAMERA_TIMEOUT,
            )
            # FFmpeg's -fs can exceed its limit by a packet; cap the bytes we read.
            if not 0 < output.stat().st_size <= CAMERA_MAX_BYTES:
                logger.warning("Camera snapshot has invalid size")
                return None
            return output.read_bytes()
    except subprocess.TimeoutExpired:
        # subprocess.run kills and reaps the child before the workspace is removed.
        logger.warning("Camera snapshot exceeded its deadline")
    except OSError, subprocess.CalledProcessError:
        # Native errors may contain source URLs; do not export their diagnostics.
        logger.warning("Camera snapshot failed")
    return None


def camera(name: str) -> Optional[bytes]:
    if name == "msu":
        for _ in range(2):
            if frame := camera_frame(CAMERA_URL):
                return frame

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
