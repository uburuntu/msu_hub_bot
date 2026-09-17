import asyncio

from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.filters.callback_data import CallbackData
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.utils.markdown import hbold

from msu_hub_bot.telegram.callbacks import CallbackCommandBase
from msu_hub_bot.telegram.filters import MetaInfo


class LikeCallback(CallbackData, prefix="like"):
    count: int


class Like(CallbackCommandBase):
    callback_data = LikeCallback

    @classmethod
    def keyboard(cls, count: int = 0) -> InlineKeyboardMarkup:
        keyboard = InlineKeyboardBuilder().row(
            InlineKeyboardButton(text=f"{count} ❤️", callback_data=LikeCallback(count=count).pack()),
        )
        return InlineKeyboardMarkup(inline_keyboard=keyboard.export())

    @classmethod
    async def process(cls, _message: Message, meta: MetaInfo) -> Message | bool | None:
        target = meta.reply()
        msg = await target.reply(hbold("Мне нравится"), reply_markup=cls.keyboard())
        return msg

    @classmethod
    async def process_cb(cls, query: CallbackQuery) -> Message | bool | None:
        message = query.message
        if not isinstance(message, Message):
            return await query.answer("Эта кнопка уже недоступна.")

        key = cls.cache_key(message)
        if key in cls.cache:
            cls.cache[key] += 1
            count = cls.cache[key]
        else:
            if not message.reply_markup:
                return await query.answer("Эта кнопка уже недоступна.")
            count = int(message.reply_markup.inline_keyboard[0][0].text.partition(" ")[0]) + 1
            cls.cache[key] = count

        await query.answer(text=f"Лайк №{count} 👍🏻")

        lock = cls.lock(key)

        if lock.locked():
            return True

        async with lock:
            await asyncio.sleep(2.0)
            return await message.edit_reply_markup(reply_markup=cls.keyboard(cls.cache.get(key, count)))
