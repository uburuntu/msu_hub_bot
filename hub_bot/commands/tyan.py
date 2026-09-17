import random

from aiogram.exceptions import TelegramBadRequest
import aiohttp
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.filters.callback_data import CallbackData
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.utils.markdown import hbold

from common import json
from common.tg.callbacks import CallbackCommandBase
from common.tg.filters import MetaInfo
from common.tg.middlewares.settings import Settings


class TyanCallback(CallbackData, prefix="tyan", sep=":"):
    type: str
    category: str


class Tyan(CallbackCommandBase):
    callback_data = TyanCallback

    @classmethod
    def keyboard(cls, with_nsfw: bool) -> InlineKeyboardMarkup:
        categories_swf = (
            "waifu",
            "neko",
            "shinobu",
            "megumin",
            "bully",
            "cuddle",
            "cry",
            "hug",
            "awoo",
            "kiss",
            "lick",
            "pat",
            "smug",
            "bonk",
            "yeet",
            "blush",
            "smile",
            "wave",
            "highfive",
            "handhold",
            "nom",
            "bite",
            "glomp",
            "slap",
            "kill",
            "kick",
            "happy",
            "wink",
            "poke",
            "dance",
            "cringe",
            "neuro",
        )
        categories_nswf = ("waifu", "neko", "trap", "blowjob")

        keyboard = InlineKeyboardBuilder()
        keyboard.row(
            *[InlineKeyboardButton(text=c.title(), callback_data=TyanCallback(type="sfw", category=c).pack()) for c in categories_swf],
            width=4,
        )
        if with_nsfw:
            keyboard.row(InlineKeyboardButton(text="⬇️ NSFW", callback_data=TyanCallback(type="nsfw", category="nsfw").pack()))
            keyboard.row(
                *[
                    InlineKeyboardButton(text=c.title(), callback_data=TyanCallback(type="nsfw", category=c).pack())
                    for c in categories_nswf
                ],
                width=4,
            )

        return InlineKeyboardMarkup(inline_keyboard=keyboard.export())

    @classmethod
    async def process(cls, _message: Message, meta: MetaInfo, settings: Settings) -> Message | bool | None:
        target = meta.reply()
        return await target.reply(hbold("База аниме тяночек 👩🏻‍🦰👱🏻‍♀️👩🏻"), reply_markup=cls.keyboard(settings.with_nsfw))

    @classmethod
    async def process_cb(cls, query: CallbackQuery, callback_data: TyanCallback, settings: Settings | None = None) -> Message | bool | None:
        message = query.message
        if not isinstance(message, Message):
            return await query.answer("Эта кнопка уже недоступна.")
        if settings is None:
            raise RuntimeError("Chat preferences middleware is required for accessible callbacks")
        type_, category = callback_data.type, callback_data.category

        if type_ == category:
            if type_ == "sfw":
                return await query.answer(text="📝 Safe for work — без эротики", cache_time=cls.cache_time_long, show_alert=True)
            return await query.answer(
                text="🔞 Not safe for work — может содержать эротику", cache_time=cls.cache_time_long, show_alert=True
            )

        if not settings.with_nsfw and type_ == "nsfw":
            return await query.answer(text="🚫", cache_time=cls.cache_time_10s)

        await query.answer(text="✅", cache_time=1)

        if category == "neuro":
            url = cls.request_neuro_tyan()
            return await message.reply_photo(url, caption=f"{hbold('Нейротянка')} для {query.from_user.mention_html()}")

        for _ in range(3):
            try:
                url = await cls.request_tyan(type_, category)
                caption = hbold(category.title()) + (f" для {query.from_user.mention_html()}" if message.chat.type != "private" else "")

                if url.endswith(("gif", "mp4")):
                    return await message.reply_video(url, caption=caption)
                return await message.reply_photo(url, caption=caption)
            except TelegramBadRequest as error:
                if not any(detail in error.message.lower() for detail in ("failed to get http url content", "wrong file identifier")):
                    raise
        return None

    @classmethod
    async def request_tyan(cls, type_: str, category: str) -> str:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"https://api.waifu.pics/{type_}/{category}") as response:
                result = await response.json(loads=json.loads)
                url = result["url"]
                if not isinstance(url, str):
                    raise ValueError("Image provider returned an invalid URL")
                return url

    @classmethod
    def request_neuro_tyan(cls) -> str:
        number = "".join(random.choices("0123456789", k=5))
        return f"https://thisanimedoesnotexist.ai/results/psi-1.0/seed{number}.png"
