from aiogram import html
from contextlib import suppress

from aiogram import Bot
from aiogram.enums import ChatAction
from aiogram.types import Message
from aiogram.exceptions import TelegramBadRequest
from aiogram.utils.markdown import hcode

from msu_hub_bot.execution.executor import TPExecutor
from msu_hub_bot.telegram.constants import TELEGRAM_MESSAGE_MAX_LEN
from msu_hub_bot.execution.sed import SedTimeout, sed_calc


async def process_sed(message: Message, bot: Bot, cpu_executor: TPExecutor) -> Message | bool | None:
    if not (reply_to := message.reply_to_message):
        return True

    text = reply_to.text or reply_to.caption
    if not text:
        return True

    await bot.send_chat_action(
        chat_id=message.chat.id, action=ChatAction.TYPING, message_thread_id=message.message_thread_id if message.is_topic_message else None
    )
    commands = (message.text or message.caption or "").split("\n")

    try:
        text, timeouted = await cpu_executor.run(sed_calc, text, commands)
    except SedTimeout:
        text, timeouted = None, True
    if timeouted:
        return await message.reply(hcode("Timeout 🤗"))
    if not text:
        return True

    with suppress(TelegramBadRequest):
        return await reply_to.reply(html.quote(text[:TELEGRAM_MESSAGE_MAX_LEN]))
    return None
