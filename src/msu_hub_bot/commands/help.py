import asyncio
from contextlib import suppress

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message, InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery
from aiogram.enums import ChatType
from aiogram.filters.callback_data import CallbackData
from aiogram.utils.keyboard import InlineKeyboardBuilder

from msu_hub_bot.telegram.callbacks import CallbackCommandBase
from msu_hub_bot.telegram.filters import MetaInfo
from msu_hub_bot.telegram.runtime import Supervisor
from msu_hub_bot.texts import cmd_help


class HelpCallback(CallbackData, prefix="help"):
    action: str


class HelpMessage(CallbackCommandBase):
    callback_data = HelpCallback
    compressed_text = "📝 Команды и возможности бота"

    @classmethod
    def keyboard(cls) -> InlineKeyboardMarkup:
        keyboard = InlineKeyboardBuilder().add(
            InlineKeyboardButton(text="⏬ Развернуть помощь", callback_data=HelpCallback(action="open").pack())
        )
        return InlineKeyboardMarkup(inline_keyboard=keyboard.export())

    @classmethod
    async def process(cls, message: Message, meta: MetaInfo, supervisor: Supervisor) -> Message:
        target = meta.reply_target()
        result = await target.reply(cmd_help, disable_web_page_preview=True)
        if message.chat.type != ChatType.PRIVATE:
            supervisor.create_job(lambda: cls.edit(result))
        return result

    @classmethod
    async def process_cb(cls, query: CallbackQuery, callback_data: HelpCallback, supervisor: Supervisor) -> bool:
        message = query.message
        if not isinstance(message, Message):
            return await query.answer("Эта кнопка уже недоступна.")
        action = callback_data.action

        await query.answer(text="✅", cache_time=1 * 60)

        if action == "open":
            with suppress(TelegramBadRequest):
                result = await message.edit_text(cmd_help, disable_web_page_preview=True)
                if isinstance(result, Message):
                    supervisor.create_job(lambda: cls.edit(result))

        return True

    @classmethod
    async def edit(cls, message: Message) -> None:
        await asyncio.sleep(60.0)
        with suppress(TelegramBadRequest):
            await message.edit_text(cls.compressed_text, reply_markup=cls.keyboard(), disable_web_page_preview=True)
