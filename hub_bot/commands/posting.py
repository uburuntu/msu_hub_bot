from msu_hub_bot.settings import settings

import asyncio

import aiogram
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import StatesGroup, State
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
from aiogram.utils.markdown import hbold, hcode

from app import bot, db
from db import EcosystemChat


class MakePostStates(StatesGroup):
    destination = State()
    waiting = State()


class MakePost:
    @classmethod
    def keyboard(cls) -> ReplyKeyboardMarkup:
        keyboard = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True, selective=True)
        keyboard.add(KeyboardButton(text='Запустить рассылку'))
        keyboard.add(KeyboardButton(text='Добавить целевой чат'))
        return keyboard

    @classmethod
    async def process(cls, message: Message, state: FSMContext):
        reply_to = message.reply_to_message

        if not reply_to:
            return await message.reply(f'Используйте команду реплаем на пост, который хотите разослать')

        async with state.proxy() as data:
            data['post_chat_id'] = reply_to.chat.id
            data['post_message_id'] = reply_to.message_id

        await MakePostStates.destination.set()
        return await message.reply(f'{hbold("Создание рассылки")}\n\n'
                                   f'Укажите где следует разместить пост, это можно сделать тремя способами:\n'
                                   f'1. Прислать юзернейм чата / канала, например: {hcode("@chat_msu")}\n'
                                   f'2. Прислать id чата / канала, например {hcode(str(settings.posting_main_chat_id or -1001234567890))}\n'
                                   f'3. Если это канал, то перешлите мне сообщение из него\n\n'
                                   f'Чтобы выйти из процесса рассылки — /cancel.')

    @classmethod
    async def process_waiting(cls, message: Message, state: FSMContext):
        if message.text == 'Запустить рассылку':
            return await cls.process_run(message, state)

        if message.text == 'Добавить целевой чат':
            await MakePostStates.destination.set()
            return await message.reply(f'{hbold("Добавление целевого чата")}\n\n'
                                       f'Укажите где следует разместить пост, это можно сделать тремя способами:\n'
                                       f'1. Прислать юзернейм чата / канала, например: {hcode("@chat_msu")}\n'
                                       f'2. Прислать id чата / канала, например {hcode(str(settings.posting_main_chat_id or -1001234567890))}\n'
                                       f'3. Если это канал, то перешлите мне сообщение из него\n\n'
                                       f'Чтобы выйти из процесса рассылки — /cancel.')

        return await message.reply(f'Выберите одно из предложенных действий или жмите /cancel')

    @classmethod
    async def process_destination(cls, message: Message, state: FSMContext):
        if message.forward_from_chat:
            dest = message.forward_from_chat.id
        else:
            dest = message.text

            if not dest:
                return await message.reply(f'Ожидаю текстовое сообщение или /cancel')

        try:
            chat = await message.bot.get_chat(dest)
        except aiogram.exceptions.TelegramAPIError as e:
            print(repr(e))
            return await message.reply(f'Что-то пошло не так: возможно у меня нет доступа к этому чату. '
                                       f'Пришлите другой или жмите /cancel.')

        async with state.proxy() as data:
            if 'dest_chat_ids' not in data:
                data['dest_chat_ids'] = []
            data['dest_chat_ids'].append(chat.id)

        await MakePostStates.waiting.set()
        return await message.reply(f'Отлично, чат {hcode(chat.full_name)} добавлен в рассылку, '
                                   f'выберите следующее действие.', reply_markup=cls.keyboard())

    @classmethod
    async def process_run(cls, message: Message, state: FSMContext):
        async with state.proxy() as data:
            post_chat_id = data['post_chat_id']
            post_message_id = data['post_message_id']
            dest_chat_ids = data['dest_chat_ids']

        await state.finish()

        for chat_id in dest_chat_ids:
            try:
                await message.bot.copy_message(chat_id, post_chat_id, post_message_id)
            except aiogram.exceptions.TelegramAPIError as e:
                chat = await bot.get_chat(chat_id)
                await message.answer(f'Возникла ошибка с чатом {hcode(chat.full_name)} (пропускаю его):\n\n'
                                     f'{hcode(repr(e))}')

        return await message.answer(f'Рассылка завершена!')


tb_chat_id = settings.posting_tb_chat_id


async def process_post_all(message: Message):
    if not (post_message := message.reply_to_message):
        return

    e_chats = await EcosystemChat.query(db).get_all()
    chat_ids = [chat.chat_id for chat in e_chats
                if chat.members >= 55 and chat.section not in ('channel',) and chat.chat_id != tb_chat_id]


    coros = [post_message.send_copy(chat_id, disable_notification=True) for chat_id in chat_ids]
    return await asyncio.gather(*coros)


async def process_post_forward_all(message: Message):
    if not (post_message := message.reply_to_message):
        return

    e_chats = await EcosystemChat.query(db).get_all()
    chat_ids = [chat.chat_id for chat in e_chats
                if chat.members >= 55 and chat.section not in ('channel',) and chat.chat_id != tb_chat_id]


    coros = [post_message.forward(chat_id, disable_notification=True) for chat_id in chat_ids]
    return await asyncio.gather(*coros)
