"""Chat-local reaction rankings with one native callback schema and redraw path."""

from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import StateFilter
from aiogram.types import Message
from cachetools import TTLCache
from teleforge.cards import Card, action, card, show
from teleforge.context import CallbackContext, Context, MessageContext
from teleforge.feature import Feature

from msu_hub_bot.commands.reactions import Days, ReactionCallback, View, keyboard, render_scoreboard
from msu_hub_bot.storage.base import BotRepository

from .command import command as hub_command

_GROUPS = {ChatType.GROUP, ChatType.SUPERGROUP}
_GROUP_GUIDANCE = "Рейтинг живёт в групповом чате: напиши там /reactions. Для сбора реакций мне нужны права администратора."


class ReactionsFeature(Feature, key="reactions"):
    def __init__(self) -> None:
        self.permissions: TTLCache[tuple[int, int], bool] = TTLCache(maxsize=512, ttl=60)

    @hub_command("reactions", "реакции", flags={"handler_key": "Reactions.process", "fsm_release": True})
    async def process(self, ctx: MessageContext) -> Message | list[Message]:
        if ctx.message.chat.type not in _GROUPS:
            return await ctx.reply(_GROUP_GUIDANCE)
        return await show(ctx, self.scoreboard, view="getters", days=30)

    @card
    async def scoreboard(self, ctx: Context, view: View, days: Days, *, db: BotRepository) -> Card:
        message = ctx.message
        assert isinstance(message, Message) and message.chat.type in _GROUPS
        board = await db.reaction_scoreboard(message.chat.id, days=days)
        content = render_scoreboard(board, message, view, administrator=await self._administrator(message.as_(ctx.bot)))
        return Card(content, buttons=keyboard(view, days))

    @action(
        key="navigate",
        card="scoreboard",
        payload=ReactionCallback,
        coalesce=True,
        filters=(StateFilter(None),),
        flags={"handler_key": "Reactions.process_cb", "fsm_release": True},
    )
    async def process_cb(self, ctx: CallbackContext) -> bool | None:
        # The wire stores only view/period. Telegram's actual clicked message is
        # the group/topic source; the card boundary verifies this bot authored it.
        if ctx.user is None or ctx.user.is_bot or not isinstance(ctx.message, Message) or ctx.message.chat.type not in _GROUPS:
            await ctx.answer("Открой /reactions в групповом чате.")
            return False
        await ctx.answer()
        return None

    async def _administrator(self, message: Message) -> bool | None:
        assert message.bot is not None
        key = (message.bot.id, message.chat.id)
        if key in self.permissions:
            return self.permissions[key]
        try:
            member = await message.bot.get_chat_member(message.chat.id, message.bot.id)
        except TelegramAPIError:
            return None
        result = member.status in {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR}
        self.permissions[key] = result
        return result
