import random
from html import escape

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.filters.callback_data import CallbackData
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.utils.markdown import hbold, hlink

from msu_hub_bot.providers.tyan import TyanImage, TyanProvider, TyanUnavailable, category_available
from msu_hub_bot.telemetry import Telemetry, record_handled_failure
from msu_hub_bot.telegram.callbacks import CallbackCommandBase
from msu_hub_bot.telegram.context import bot_for
from msu_hub_bot.telegram.filters import MetaInfo
from msu_hub_bot.telegram.middlewares.settings import Settings


class TyanCallback(CallbackData, prefix="tyan", sep=":"):
    type: str
    category: str


class Tyan(CallbackCommandBase):
    callback_data = TyanCallback
    images = TyanProvider()

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
    async def process_cb(
        cls,
        query: CallbackQuery,
        callback_data: TyanCallback,
        settings: Settings | None = None,
        telemetry: Telemetry | None = None,
    ) -> Message | bool | None:
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

        if type_ == "sfw" and category == "neuro":
            await query.answer(cache_time=1)
            url = cls.request_neuro_tyan()
            return await message.reply_photo(url, caption=f"{hbold('Нейротянка')} для {query.from_user.mention_html()}")

        if not category_available(type_, category):
            return await query.answer("Эта категория сейчас недоступна. Выбери другую.", show_alert=True)

        await query.answer(cache_time=1)
        for attempt in range(3):
            try:
                image = await cls.request_tyan(type_, category, telemetry=telemetry)
            except TyanUnavailable as error:
                record_handled_failure(error)
                # The button was already acknowledged. Use a durable reply so
                # a provider outage cannot disappear with an expired callback.
                return await message.reply(error.text, parse_mode=None)
            try:
                caption = hbold(category.title()) + (f" для {query.from_user.mention_html()}" if message.chat.type != "private" else "")
                credits = []
                if image.artist_name:
                    credits.append(hlink(image.artist_name, escape(image.artist_url)) if image.artist_url else hbold(image.artist_name))
                if image.source_url:
                    credits.append(hlink("Источник", escape(image.source_url)))
                if image.anime_name:
                    credits.append(hbold(image.anime_name))
                if credits:
                    caption += "\n" + " · ".join(credits)
                method = (
                    message.reply_animation(image.url, caption=caption)
                    if image.animated
                    else message.reply_photo(image.url, caption=caption)
                )
                sent: Message = await bot_for(message)(method, request_timeout=15)
                return sent
            except TelegramBadRequest as error:
                if not any(detail in error.message.lower() for detail in ("failed to get http url content", "wrong file identifier")):
                    raise
                if attempt == 2:
                    record_handled_failure(error)
        return await message.reply("Не получилось загрузить картинку. Попробуй другую категорию.", parse_mode=None)

    @classmethod
    async def request_tyan(cls, type_: str, category: str, *, telemetry: Telemetry | None = None) -> TyanImage:
        return await cls.images.image(type_, category, telemetry=telemetry)

    @classmethod
    def request_neuro_tyan(cls) -> str:
        number = "".join(random.choices("0123456789", k=5))
        return f"https://thisanimedoesnotexist.ai/results/psi-1.0/seed{number}.png"
