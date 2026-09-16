import io
from contextlib import suppress

import aiogram
import pytesseract
from PIL import Image
from aiogram.types import InputFile, Message
from aiogram.utils.markdown import hcode, quote_html

from app import cpu_executor
from common.constants import TELEGRAM_MESSAGE_MAX_LEN
from common.tg.filters import MetaInfo
from common.utils import strip_blank_rows, valid_filename


def to_text(file: io.BytesIO):
    image = Image.open(file)
    text = pytesseract.image_to_string(image, lang='rus+eng')
    return text


async def process_image_to_text(message: Message, meta: MetaInfo):
    target, file = await meta.extract_image_with_downloading()
    if file is None:
        return True

    text, timeouted = await cpu_executor.run(to_text, file)
    if timeouted:
        return await message.reply(hcode('🤷🏻‍♂️ Timeout'))
    if not text:
        return await message.reply(hcode('🤷🏻‍♂️ Текст не найден'))

    if len(text) > TELEGRAM_MESSAGE_MAX_LEN:
        txt = io.BytesIO(bytes(text, encoding='utf-8'))
        txt.seek(0)
        txt.name = f'ocr_{valid_filename(text, 20).lower()}.txt'
        return await target.reply_document(InputFile(txt))

    with suppress(aiogram.exceptions.BadRequest):
        return await target.reply(quote_html(strip_blank_rows(text)))

    return await message.reply(hcode('🤷🏻‍♂️ Текст не найден'))
