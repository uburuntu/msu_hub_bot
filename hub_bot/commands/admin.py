from collections.abc import Awaitable, Callable
from contextlib import suppress

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import ChatMemberAdministrator, ChatMemberOwner, ChatPermissions, Message
from aiogram.utils.markdown import hcode, hpre

from common.db.edb import EdgeDB
from common.tg.runtime import gather_complete
from common.tg.utils import chat_link, command_arguments
from common.tg.wrapper import BotWrapper
from hub_bot.db import EcosystemChat


async def process_ban(message: Message, bot: BotWrapper, db: EdgeDB) -> list[bool] | None:
    argument = command_arguments(message)
    if not argument.isdigit():
        return None
    chats = await EcosystemChat.query(db).get_all()
    return await gather_complete(*(bot.ban_chat_member(chat.chat_id, int(argument)) for chat in chats))


async def process_restrict(message: Message, bot: BotWrapper, db: EdgeDB) -> list[bool] | None:
    argument = command_arguments(message)
    if not argument.isdigit():
        return None
    chats = await EcosystemChat.query(db).get_all()
    return await gather_complete(*(bot.restrict_chat_member(chat.chat_id, int(argument), ChatPermissions()) for chat in chats))


async def process_unban(message: Message, bot: BotWrapper, db: EdgeDB) -> list[bool] | None:
    argument = command_arguments(message)
    if not argument.isdigit():
        return None
    chats = await EcosystemChat.query(db).get_all()
    return await gather_complete(*(bot.unban_chat_member(chat.chat_id, int(argument), only_if_banned=True) for chat in chats))


async def process_sudo(message: Message, bot: BotWrapper, events_chat_id: int) -> bool | None:
    if message.from_user is None:
        return None
    admins = await bot.get_chat_administrators(message.chat.id)
    me = next((admin for admin in admins if admin.user.id == bot.id), None)
    if not isinstance(me, ChatMemberAdministrator):
        return None
    if me.can_delete_messages:
        await message.delete()
    if not me.can_promote_members or any(admin.user.id == message.from_user.id for admin in admins):
        return None
    result = await bot.promote_chat_member(
        message.chat.id,
        message.from_user.id,
        can_change_info=True,
        can_delete_messages=True,
        can_invite_users=True,
        can_restrict_members=True,
        can_pin_messages=True,
        can_promote_members=False,
    )
    if result and events_chat_id:
        event = (
            f"🌝 {message.from_user.mention_html()} воспользовался командой {hcode('sudo')} в чате {await chat_link(message.chat, True)}."
        )
        await bot.send_message(events_chat_id, event, disable_web_page_preview=True)
    return result


async def process_revoke(message: Message, bot: BotWrapper, events_chat_id: int) -> bool | None:
    if message.from_user is None:
        return None
    admins = await bot.get_chat_administrators(message.chat.id)
    me = next((admin for admin in admins if admin.user.id == bot.id), None)
    if not isinstance(me, ChatMemberAdministrator):
        return None
    if me.can_delete_messages:
        await message.delete()
    if not me.can_promote_members:
        return None
    target = next((admin for admin in admins if admin.user.id == message.from_user.id), None)
    if target is None or isinstance(target, ChatMemberOwner):
        return None
    result = await bot.promote_chat_member(message.chat.id, message.from_user.id)
    if result and events_chat_id:
        event = (
            f"🌚 {message.from_user.mention_html()} воспользовался командой {hcode('revoke')} в чате {await chat_link(message.chat, True)}."
        )
        await bot.send_message(events_chat_id, event, disable_web_page_preview=True)
    return result


async def process_forwards(message: Message, bot: BotWrapper) -> Message | None:
    args = command_arguments(message).split()
    if len(args) != 3:
        return await message.reply(hpre("/forwards [chat_id] [from_message_id] [count]"))
    chat_id = args[0]
    first, count = int(args[1]), int(args[2])
    for message_id in range(first, first + count):
        with suppress(TelegramBadRequest):
            await bot.forward_message(message.chat.id, chat_id, message_id)
    return None


def process_forward_builder(dest_chat_id: int) -> Callable[[Message], Awaitable[Message | None]]:
    async def process_forward(message: Message) -> Message | None:
        with suppress(TelegramBadRequest):
            return await message.forward(dest_chat_id)
        return None

    return process_forward
