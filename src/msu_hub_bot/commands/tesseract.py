from aiogram import html
import io
from contextlib import suppress

import pytesseract
from PIL import Image
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message
from aiogram.utils.markdown import hcode

from msu_hub_bot.execution.executor import TPExecutor
from msu_hub_bot.telegram.constants import TELEGRAM_MESSAGE_MAX_LEN
from msu_hub_bot.telegram.filters import MetaInfo
from msu_hub_bot.telegram.files import input_file
from msu_hub_bot.utils import strip_blank_rows, valid_filename


def to_text(file: io.BytesIO) -> str:
    image = Image.open(file)
    text = pytesseract.image_to_string(image, lang="rus+eng")
    return str(text)


async def process_image_to_text(message: Message, meta: MetaInfo, cpu_executor: TPExecutor) -> Message | bool:
    target, file = await meta.extract_image_with_downloading()
    if file is None:
        return True

    text, timeouted = await cpu_executor.run(to_text, file)
    if timeouted:
        return await message.reply(hcode("🤷🏻‍♂️ Timeout"))
    if not text:
        return await message.reply(hcode("🤷🏻‍♂️ Текст не найден"))

    if len(text) > TELEGRAM_MESSAGE_MAX_LEN:
        txt = io.BytesIO(bytes(text, encoding="utf-8"))
        txt.seek(0)
        txt.name = f"ocr_{valid_filename(text, 20).lower()}.txt"
        return await target.reply_document(input_file(txt, txt.name))

    with suppress(TelegramBadRequest):
        return await target.reply(html.quote(strip_blank_rows(text)))

    return await message.reply(hcode("🤷🏻‍♂️ Текст не найден"))
