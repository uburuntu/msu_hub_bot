from msu_hub_bot.settings import settings

import datetime
from contextlib import suppress

import aiogram.utils.exceptions
from aiogram.types import CallbackQuery, Message
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.callback_data import CallbackData
from aiogram.utils.markdown import hbold, hlink
from emoji import emoji_count

from app import redis
from common.cas import CombotAntiSpam
from common.tg.callbacks import CallbackCommandBase


class AntiBot(CallbackCommandBase):
    callback_data = CallbackData('antibot', 'action', 'chat_id', 'user_id')

    @classmethod
    def keyboard(cls, chat_id: int, user_id: int) -> InlineKeyboardMarkup:
        keyboard = InlineKeyboardMarkup()
        keyboard.add(
            InlineKeyboardButton(text='🔨 Бан', callback_data=cls.callback_data.new('ban', chat_id, user_id)),
            InlineKeyboardButton(text='👊🏻 Кик', callback_data=cls.callback_data.new('kick', chat_id, user_id)),
        ).add(
            InlineKeyboardButton(text='🆗 Оставить', callback_data=cls.callback_data.new('ignore', chat_id, user_id)),
        )
        return keyboard

    @classmethod
    def keyboard_after_decision(cls, chat_id: int, user_id: int) -> InlineKeyboardMarkup:
        keyboard = InlineKeyboardMarkup().add(
            InlineKeyboardButton(text='🆗 Закрыть', callback_data=cls.callback_data.new('close', chat_id, user_id)),
        )
        return keyboard

    @classmethod
    async def process(cls, message: Message):
        for user in message.new_chat_members:
            member = await message.chat.get_member(user.id)
            if member.is_chat_admin():
                # Skip owner
                continue

            reasons = set()

            if 'iherb' in user.full_name.lower():
                reasons.add('реклама в профиле')

            if user.id > settings.antibot_user_id:
                if emoji_count(user.full_name) >= 3:
                    reasons.add('много эмодзи в имени')

            if emoji_count(user.full_name) >= 4:
                reasons.add('много эмодзи в имени')

            # Long responses
            if not reasons:
                if await CombotAntiSpam.banned(user.id):
                    link = hlink('CAS', f'https://cas.chat/query?u={user.id}')
                    reasons.add(f'бан в системе {link}')

            if reasons:
                reason = ('причины: ' if len(reasons) > 1 else 'причина: ') + ', '.join(reasons)
                reply = await message.reply(f'⚠️ Подозреваю, что {user.get_mention()} — {hbold("бот")}, {reason}',
                                            reply_markup=cls.keyboard(message.chat.id, user.id))
                await redis.mark_message_to_delete(reply, after=24 * 60 * 60)

        return True

    @classmethod
    async def process_left(cls, message: Message):
        if message.from_user.id == message.bot.id:
            with suppress(aiogram.exceptions.BadRequest):
                await message.delete()

    @classmethod
    async def process_cb(cls, query: CallbackQuery, callback_data: dict):
        try:
            chat_id, user_id = int(callback_data['chat_id']), int(callback_data['user_id'])
            action = callback_data['action']
        except (KeyError, TypeError, ValueError):
            return await query.answer('Эта кнопка больше не работает.')
        if not query.message or query.message.chat.id != chat_id or user_id <= 0 or action not in {'ban', 'kick', 'ignore', 'close'}:
            return await query.answer('Эта кнопка больше не работает.')
        async with cls.lock(cls.cache_key(query.message)):
            return await cls._process_cb_locked(query, callback_data)

    @classmethod
    async def _process_cb_locked(cls, query: CallbackQuery, callback_data: dict):
        action, chat_id, user_id = callback_data['action'], int(callback_data['chat_id']), int(callback_data['user_id'])
        reply = query.message

        member = await reply.bot.get_chat_member(chat_id, query.from_user.id)
        if not member.is_chat_admin():
            return await query.answer('❇️ Вам нужно быть администратором в чате')

        if action in {'ban', 'kick'} and not (member.is_chat_creator() or member.can_restrict_members):
            return await query.answer('❇️ Для этого нужно право блокировать участников', show_alert=True)

        key = cls.cache_key(reply)
        if action != 'close' and key in cls.cache:
            return await query.answer('Решение уже принято.')

        if action == 'ignore':
            await reply.edit_text(reply.html_text + f'\n\n{query.from_user.get_mention()} вынес вердикт: {hbold("проигнорировать")}',
                                  reply_markup=cls.keyboard_after_decision(chat_id, user_id))
            cls.cache[key] = action
            await redis.mark_message_to_delete(reply, after=60)
            return await query.answer('✅')

        if action == 'close':
            await reply.delete()
            return await query.answer('✅')

        bot_member = await reply.bot.get_chat_member(chat_id, reply.bot.id)
        if not bot_member.is_chat_admin():
            return await query.answer('❇️ Боту нужны права на бан и удаление сообщений в этом чате',
                                      cache_time=cls.cache_time_10s, show_alert=True)

        # bot_member: ChatMemberAdministrator
        if not (bot_member.can_restrict_members and bot_member.can_delete_messages):
            return await query.answer('❇️ Боту нужны права на бан и удаление сообщений в этом чате',
                                      cache_time=cls.cache_time_10s, show_alert=True)

        target = await reply.bot.get_chat_member(chat_id, user_id)
        if target.is_chat_admin():
            return await query.answer('Администраторов нельзя заблокировать этой кнопкой.', show_alert=True)
        if not target.is_chat_member() or (target.status == 'restricted' and not target.is_member):
            return await query.answer('Этот участник уже покинул чат.')

        await query.answer('✅', cache_time=cls.cache_time_10s)

        if action == 'kick':
            until_date = datetime.timedelta(minutes=1)
            verdict = f'👊🏻 {hbold("кикнуть")}'
        else:
            until_date = None
            verdict = f'🔨 {hbold("бан")}'

        await reply.bot.kick_chat_member(chat_id, user_id, until_date=until_date)
        cls.cache[key] = action
        if reply.reply_to_message:
            with suppress(aiogram.exceptions.BadRequest):
                await reply.reply_to_message.delete()
        await reply.edit_text(reply.html_text + f'\n\n{query.from_user.get_mention()} вынес вердикт: {verdict}',
                              reply_markup=cls.keyboard_after_decision(chat_id, user_id))
        await redis.mark_message_to_delete(reply, after=60)
        return True
