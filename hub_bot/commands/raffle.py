import asyncio
import random

from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, User
from aiogram.filters.callback_data import CallbackData
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.utils.markdown import hbold, hitalic

from common.tg.callbacks import CallbackCommandBase
from common.tg.utils import profile_photo, username_mention, sender_mention
from common.utils import outdated


def sample(population: list[User], k: int) -> list[User]:
    if len(population) < k:
        return random.choices(population, k=k)
    return random.sample(population, k=k)


class RaffleCallback(CallbackData, prefix="raffle"):
    action: str


class Raffle(CallbackCommandBase):
    callback_data = RaffleCallback

    @classmethod
    def keyboard(cls) -> InlineKeyboardMarkup:
        keyboard = (
            InlineKeyboardBuilder()
            .row(
                InlineKeyboardButton(text="Участвую! 🎈", callback_data=RaffleCallback(action="reg").pack()),
            )
            .row(
                InlineKeyboardButton(text="Выбрать победителя 👑", callback_data=RaffleCallback(action="winner").pack()),
            )
        )
        return InlineKeyboardMarkup(inline_keyboard=keyboard.export())

    @classmethod
    async def process(cls, message: Message) -> Message | bool | None:
        target = message.reply_to_message if message.reply_to_message else message
        text = (
            "✨ "
            + hbold("Конкурс")
            + f" от {username_mention(message.from_user) if message.from_user else sender_mention(message)}\n\nУчастники:"
        )
        return await target.reply(text, reply_markup=cls.keyboard())

    @classmethod
    async def process_cb(cls, query: CallbackQuery, callback_data: RaffleCallback) -> Message | bool | None:
        m = query.message
        if not isinstance(m, Message):
            return await query.answer("Эта кнопка уже недоступна.")

        if outdated(m.date + cls.cache_time_long_td):
            await query.answer(text="♻️ Этому розыгрышу больше недели, он закрывается", show_alert=True)
            return await m.edit_reply_markup(reply_markup=None)

        action = callback_data.action

        if action == "reg":
            await query.answer(text="🎈 Вы участвуете в розыгрыше!", cache_time=cls.cache_time_long)
            text_part = f"\n— {query.from_user.mention_html()}"
            text = await cls.cached_text(m, text_part)
            async with cls.lock(m):
                return await m.edit_text(text, reply_markup=m.reply_markup)

        participants = [e.user for e in (m.entities or []) if e.user]
        if not participants:
            return await query.answer("Этот розыгрыш уже недоступен.")
        creator, *users = participants
        if query.from_user != creator:
            return await query.answer("🤷🏻‍♂️ Только создатель розыгрыша может завершить его", cache_time=cls.cache_time_long)
        if len(users) == 0:
            return await query.answer("🤷🏻‍♂️ Нельзя завершить розыгрыш без участников", cache_time=3)

        await query.answer("👑 Запущено определение победителя!", cache_time=cls.cache_time_long)
        await m.edit_reply_markup(reply_markup=None)

        async with cls.lock(m):
            message = await m.reply(hitalic("👑 Запущено определение победителя!"))

            u1, u2 = sample(users, 2)
            winner = random.choice(users)
            photo = await profile_photo(winner)
            text = hbold("👑 Победитель:") + f" {winner.mention_html()}"

            for t in (
                hitalic("👑 Может это будет") + f" {u1.mention_html()}?",
                hitalic("👑 Или") + f" {u2.mention_html()}?",
                hitalic("👑 Сейчас и узнаем! Итак, победитель..."),
            ):
                await asyncio.sleep(3.0)
                await message.edit_text(t)

            await asyncio.sleep(3.0)
            await message.delete()
            if photo:
                message = await m.reply_photo(photo.file_id, caption=text)
            else:
                message = await m.reply(text)

            await m.edit_text(m.html_text + f"\n\n{text}")

        return message
