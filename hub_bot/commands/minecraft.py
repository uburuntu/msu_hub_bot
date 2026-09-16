import asyncio
import socket
from contextlib import suppress

from aiogram.exceptions import TelegramBadRequest
from common.caching import cached_async
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters.callback_data import CallbackData
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.utils.markdown import hbold, hide_link
from mcstatus import JavaServer, BedrockServer

from common.tg.callbacks import CallbackCommandBase
from common.tg.filters import MetaInfo
from common.tg.utils import extract_urls
from common.utils import random_cycle


class MinecraftCallback(CallbackData, prefix="minecraft", sep="$"):
    url: str


class MinecraftStatus(CallbackCommandBase):
    callback_data = MinecraftCallback

    server_url = 'minecraft.msut.me'
    screenshots = random_cycle(
        'https://i.imgur.com/AA3fgbf.png',
        'https://i.imgur.com/46hDyUg.png',
        'https://i.imgur.com/MJLh8p5.png',
        'https://i.imgur.com/6TJSTPf.png',
        'https://i.imgur.com/1bi3Aki.png',
        'https://i.imgur.com/cz3pJyf.png',
        'https://i.imgur.com/eN5uYW8.png',
        'https://i.imgur.com/PRZpGY0.png',
        'https://i.imgur.com/31Q32G0.png',
        'https://i.imgur.com/AdB5KwK.png',
        'https://i.imgur.com/c6XCbU3.png',
    )

    @classmethod
    def keyboard(cls, url: str) -> InlineKeyboardMarkup:
        keyboard = InlineKeyboardBuilder().row(
            InlineKeyboardButton(text='🔄 Обновить', callback_data=MinecraftCallback(url=url).pack()),
        )
        if url == cls.server_url:
            keyboard.add(
                InlineKeyboardButton(text='🗒 Подробнее', url='https://vk.com/wall13628232_1332'),
            )
        return InlineKeyboardMarkup(inline_keyboard=keyboard.export())

    @classmethod
    async def process(cls, _message: Message, meta: MetaInfo) -> Message | bool | None:
        url = cls.server_url

        target, _ = meta.extract_text()
        if urls := extract_urls(target, include_text_link=False):
            url = str(urls[0][0].with_path(''))[len('https://'):]

        text = await cls.mc_status(url)
        return await target.reply(text, reply_markup=cls.keyboard(url))

    @classmethod
    async def process_cb(cls, query: CallbackQuery, callback_data: MinecraftCallback) -> Message | bool | None:
        message = query.message
        if not isinstance(message, Message):
            return await query.answer("Эта кнопка уже недоступна.")
        await query.answer(text='✅', cache_time=30)

        url = callback_data.url
        text = await cls.mc_status(url)

        with suppress(TelegramBadRequest):
            return await message.edit_text(text, reply_markup=cls.keyboard(url))
        return None

    @classmethod
    @cached_async(ttl=30, noself=True)
    async def mc_status(cls, url: str) -> str:
        try:
            mc = JavaServer.lookup(url)
            status = await mc.async_status()
        except (asyncio.TimeoutError, ConnectionError, socket.gaierror):
            status = None

        text = ''
        if url == cls.server_url:
            text += hide_link(next(cls.screenshots))
            text += hbold('Minecraft сервер МГУ | @minecraft_msu 🏰') + '\n\n'
        else:
            text += hbold('Minecraft сервер') + '\n\n'

        text += hbold('Адрес') + f': {url}\n\n'

        if status:
            if status.players.online:
                text += '👥 ' + hbold('Игроков') + f' ({status.players.online} / {status.players.max})'
                if status.players.sample:
                    text += ':\n— ' + '\n— '.join(p.name for p in status.players.sample[:10])
                text += '\n\n'
            else:
                text += f'👥 Сейчас на сервере нет игроков\n\n'

            text += hbold('Версия') + f': {status.version.name}\n'
            text += hbold('Пинг') + f': {int(status.latency)} мс\n'

        else:
            text += 'Сервер сейчас ' + hbold('оффлайн') + ' 😴\n'

        return text
