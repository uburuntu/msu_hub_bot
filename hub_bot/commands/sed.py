from contextlib import suppress

from aiogram.types import ChatActions, Message
from aiogram.utils.exceptions import BadRequest
from aiogram.utils.markdown import hcode, quote_html

from app import cpu_executor
from common.constants import TELEGRAM_MESSAGE_MAX_LEN
from utils.sed import SedTimeout, sed_calc


async def process_sed(message: Message):
    if not (reply_to := message.reply_to_message):
        return True

    text = reply_to.text or reply_to.caption
    if not text:
        return True

    await message.chat.do(ChatActions.TYPING)
    commands = message.text.split('\n')

    try:
        text, timeouted = await cpu_executor.run(sed_calc, text, commands)
    except SedTimeout:
        text, timeouted = None, True
    if timeouted:
        return await message.reply(hcode('Timeout 🤗'))
    if not text:
        return True

    with suppress(BadRequest):
        return await reply_to.reply(quote_html(text[:TELEGRAM_MESSAGE_MAX_LEN]))
