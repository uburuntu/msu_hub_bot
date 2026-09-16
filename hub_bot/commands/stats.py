
import asyncio
import datetime
from textwrap import dedent

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message, CallbackQuery
from aiogram.utils.callback_data import CallbackData
from aiogram.utils.markdown import hbold

from app import db
from common.db.edb import UserDB, ChatDB, UpdateDB
from common.tg.callbacks import CallbackCommandBase


class Stats(CallbackCommandBase):
    callback_data = CallbackData('stats', 'action')

    @classmethod
    def keyboard(cls) -> InlineKeyboardMarkup:
        keyboard = InlineKeyboardMarkup().row(
            InlineKeyboardButton(text='🔄 Обновить', callback_data=cls.callback_data.new('update')),
        )
        return keyboard

    @classmethod
    async def text(cls):
        yesterday = datetime.datetime.utcnow() - datetime.timedelta(days=1)

        coros = [
            UserDB.query(db).count(),
            ChatDB.query(db).count(),
            UpdateDB.query(db).count(f'.created > to_datetime({int(yesterday.timestamp())}) and .handled = true'),
            UpdateDB.query(db).count(f'.created > to_datetime({int(yesterday.timestamp())})'),
        ]

        users, chats, updates_handled, updates = await asyncio.gather(*coros)

        text = dedent(f"""
            {hbold("Статистика @msu_hub_bot")}
            
            • Знаю {hbold(chats)} чатов
            • Видел {hbold(users)} пользователей
            • За последний день обработал {hbold(updates_handled)} команд
            • И увидел {hbold(updates)} сообщений
        """).strip()
        return text

    @classmethod
    async def process(cls, message: Message):
        return await message.reply(await cls.text(), reply_markup=cls.keyboard())

    @classmethod
    async def process_cb(cls, query: CallbackQuery):
        await query.answer('✅', cache_time=1)

        lock = cls.lock(query.message)

        if lock.locked():
            return True

        async with lock:
            await query.message.edit_text(await cls.text(), reply_markup=cls.keyboard())
            await asyncio.sleep(1.)

        return True
