"""Fresh Telegram authorization bound to a user-signed chat/topic launch."""

from dataclasses import dataclass
from datetime import datetime

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import ChatMemberRestricted
from aiogram.utils.chat_member import ADMINS, MEMBERS

from .links import Destination, WebAppLinks


class AccessDenied(ValueError):
    def __init__(self) -> None:
        super().__init__("Открой /app в нужном чате. Для управления нужны права администратора у тебя и у бота.")


@dataclass(frozen=True)
class ChatAccess:
    destination: Destination
    member: bool
    admin: bool
    bot_admin: bool

    def require(self, *, admin: bool = False) -> Destination:
        if not self.member or not self.bot_admin or (admin and not self.admin):
            raise AccessDenied()
        return self.destination


async def chat_access(bot: Bot, links: WebAppLinks, user_id: int, launch: str | None, *, now: datetime) -> ChatAccess:
    destination = links.destination(user_id, launch, now=now)
    if destination.chat_id > 0:
        return ChatAccess(destination, member=True, admin=True, bot_admin=True)
    try:
        # getChatMember is only guaranteed for other users when the bot is an
        # administrator. Never use an old launch token as membership evidence.
        me = await bot.get_chat_member(destination.chat_id, bot.id)
        if not isinstance(me, ADMINS):
            return ChatAccess(destination, member=False, admin=False, bot_admin=False)
        member = await bot.get_chat_member(destination.chat_id, user_id)
    except TelegramAPIError:
        return ChatAccess(destination, member=False, admin=False, bot_admin=False)
    return ChatAccess(
        destination,
        member=isinstance(member, MEMBERS) and (not isinstance(member, ChatMemberRestricted) or member.is_member),
        admin=isinstance(member, ADMINS),
        bot_admin=True,
    )
