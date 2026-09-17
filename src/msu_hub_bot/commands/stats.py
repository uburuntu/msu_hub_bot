import asyncio
import datetime
from textwrap import dedent

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message, CallbackQuery
from aiogram.filters.callback_data import CallbackData
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.utils.markdown import hbold

from msu_hub_bot.storage.base import BotRepository
from msu_hub_bot.telegram.callbacks import CallbackCommandBase


class StatsCallback(CallbackData, prefix="stats", sep=":"):
    action: str


class Stats(CallbackCommandBase):
    callback_data = StatsCallback

    @classmethod
    def keyboard(cls) -> InlineKeyboardMarkup:
        keyboard = InlineKeyboardBuilder().row(
            InlineKeyboardButton(text="🔄 Обновить", callback_data=StatsCallback(action="update").pack()),
        )
        return InlineKeyboardMarkup(inline_keyboard=keyboard.export())

    @classmethod
    async def text(cls, db: BotRepository) -> str:
        yesterday = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=1)
        counts = await db.statistics(yesterday)

        text = dedent(f"""
            {hbold("Статистика @msu_hub_bot")}
            
            • Знаю {hbold(counts.chats)} чатов
            • Видел {hbold(counts.users)} пользователей
            • За последний день обработал {hbold(counts.handled_updates)} команд
            • И увидел {hbold(counts.updates)} сообщений
        """).strip()
        return text

    @classmethod
    async def process(cls, message: Message, db: BotRepository) -> Message | bool | None:
        return await message.reply(await cls.text(db), reply_markup=cls.keyboard())

    @classmethod
    async def process_cb(cls, query: CallbackQuery, db: BotRepository) -> Message | bool | None:
        message = query.message
        if not isinstance(message, Message):
            return await query.answer("Эта кнопка уже недоступна.")
        await query.answer("✅", cache_time=1)

        lock = cls.lock(message)

        if lock.locked():
            return True

        async with lock:
            await message.edit_text(await cls.text(db), reply_markup=cls.keyboard())
            await asyncio.sleep(1.0)

        return True
