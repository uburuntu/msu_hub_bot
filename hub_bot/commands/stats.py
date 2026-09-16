
import asyncio
import datetime
from textwrap import dedent

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message, CallbackQuery
from aiogram.filters.callback_data import CallbackData
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.utils.markdown import hbold

from common.db.edb import EdgeDB, UserDB, ChatDB, UpdateDB
from common.tg.callbacks import CallbackCommandBase
from common.tg.runtime import gather_complete


class StatsCallback(CallbackData, prefix="stats", sep=":"):
    action: str


class Stats(CallbackCommandBase):
    callback_data = StatsCallback

    @classmethod
    def keyboard(cls) -> InlineKeyboardMarkup:
        keyboard = InlineKeyboardBuilder().row(
            InlineKeyboardButton(text='🔄 Обновить', callback_data=StatsCallback(action='update').pack()),
        )
        return InlineKeyboardMarkup(inline_keyboard=keyboard.export())

    @classmethod
    async def text(cls, db: EdgeDB) -> str:
        yesterday = datetime.datetime.utcnow() - datetime.timedelta(days=1)

        coros = [
            UserDB.query(db).count(),
            ChatDB.query(db).count(),
            UpdateDB.query(db).count(f'.created > to_datetime({int(yesterday.timestamp())}) and .handled = true'),
            UpdateDB.query(db).count(f'.created > to_datetime({int(yesterday.timestamp())})'),
        ]

        users, chats, updates_handled, updates = await gather_complete(*coros)

        text = dedent(f"""
            {hbold("Статистика @msu_hub_bot")}
            
            • Знаю {hbold(chats)} чатов
            • Видел {hbold(users)} пользователей
            • За последний день обработал {hbold(updates_handled)} команд
            • И увидел {hbold(updates)} сообщений
        """).strip()
        return text

    @classmethod
    async def process(cls, message: Message, db: EdgeDB) -> Message | bool | None:
        return await message.reply(await cls.text(db), reply_markup=cls.keyboard())

    @classmethod
    async def process_cb(cls, query: CallbackQuery, db: EdgeDB) -> Message | bool | None:
        message = query.message
        if not isinstance(message, Message):
            return await query.answer('Эта кнопка уже недоступна.')
        await query.answer('✅', cache_time=1)

        lock = cls.lock(message)

        if lock.locked():
            return True

        async with lock:
            await message.edit_text(await cls.text(db), reply_markup=cls.keyboard())
            await asyncio.sleep(1.)

        return True
