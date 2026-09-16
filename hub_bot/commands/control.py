"""Conversation controls and the final, redacted user-error boundary."""

import logging
import traceback
from contextlib import suppress

from aiogram import Bot, html
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, ErrorEvent, InlineKeyboardButton, InlineKeyboardMarkup, Message, ReplyKeyboardRemove, Update
from aiogram.utils.markdown import hbold, hpre
from aiohttp import ClientError
from cachetools import TTLCache

from common.externals.exceptions import ExternalServiceError
from common.tg.state import UpdateStateContext, release_state_isolation
from hub_bot.commands.debug import process_json
from hub_bot.texts import cmd_start
from msu_hub_bot.redaction import redact
from msu_hub_bot.settings import MissingIntegration, settings

logger = logging.getLogger(__name__)
errors: TTLCache[str, bool] = TTLCache(256, ttl=60)


async def process_start(message: Message) -> Message:
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Добавь меня в любой чат ↩️", url="https://t.me/msu_hub_bot?startgroup=true")]]
    )
    return await message.reply(cmd_start, reply_markup=keyboard, disable_web_page_preview=True)


async def process_cancel(message: Message, state: FSMContext, state_context: UpdateStateContext) -> Message:
    await state.clear()
    release_state_isolation(state_context)
    return await message.reply("👌🏻", reply_markup=ReplyKeyboardRemove())


async def process_echo(message: Message) -> Message:
    await process_json(message)
    return await message.send_copy(message.chat.id)


async def reply_error(update: Update, text: str) -> None:
    with suppress(TelegramAPIError):
        if update.callback_query:
            await update.callback_query.answer(text, show_alert=True)
        elif message := update.message or update.edited_message or update.channel_post or update.edited_channel_post:
            await message.reply(html.quote(text))


async def process_expired_callback(query: CallbackQuery) -> None:
    with suppress(TelegramAPIError):
        await query.answer("Эта кнопка больше не работает. Вызовите команду заново.")


async def process_error(event: ErrorEvent, bot: Bot) -> bool:
    update, error = event.update, event.exception
    if isinstance(error, MissingIntegration):
        await reply_error(update, "Эта функция пока не настроена на этом экземпляре бота.")
        return True
    if isinstance(error, (ExternalServiceError, ClientError, TimeoutError)):
        logger.warning("External request failed: %s", redact(repr(error)))
        if isinstance(error, ExternalServiceError):
            text = redact(error.text)
        elif isinstance(error, TimeoutError):
            text = "Сервис не успел ответить. Попробуйте ещё раз позже."
        else:
            text = "Не удалось связаться с сервисом. Попробуйте ещё раз позже."
        await reply_error(update, text)
        return True

    error_str = redact(repr(error))
    trace = redact("".join(traceback.format_exception(type(error), error, error.__traceback__)))
    logger.error("Telegram update failed: %s\n%s", error_str, trace)
    if isinstance(error, TelegramForbiddenError):
        return True
    if isinstance(error, TelegramBadRequest) and any(
        fragment in error.message.lower() for fragment in ("message to edit not found", "message was deleted", "replied message not found")
    ):
        return True
    if error_str not in errors and settings.error_chat_id:
        text = hbold("Exception") + ": " + hpre(error_str[:1000]) + "\n" + hpre(trace[-2000:])
        with suppress(TelegramAPIError):
            await bot.send_message(settings.error_chat_id, text)
    errors[error_str] = True
    return True
