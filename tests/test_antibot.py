"""Moderation checks use typed membership/callback models and no Telegram calls."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendMessage
from aiogram.types import (
    ChatMemberAdministrator,
    ChatMemberBanned,
    ChatMemberLeft,
    ChatMemberMember,
    ChatMemberOwner,
    ChatMemberRestricted,
    Message,
    User,
)
from pydantic import ValidationError

from msu_hub_bot.commands.antibot import AntiBot, AntiBotCallback


@pytest.fixture(autouse=True)
def isolated():
    AntiBot.cache.clear()
    AntiBot.locks.clear()


def member(status, can_restrict=False, can_delete=False):
    classes = {
        "creator": ChatMemberOwner,
        "administrator": ChatMemberAdministrator,
        "member": ChatMemberMember,
        "restricted": ChatMemberRestricted,
        "left": ChatMemberLeft,
        "kicked": ChatMemberBanned,
    }
    cls = classes[status]
    fields = {key: False for key, value in cls.model_fields.items() if value.annotation is bool}
    fields.update(status=status, user=User(id=1, is_bot=False, first_name="Friend"), until_date=0)
    if status == "administrator":
        fields.update(can_restrict_members=can_restrict, can_delete_messages=can_delete)
    return cls.model_validate(fields)


def callback(actor, target=None, action="ban", bot_permissions=True):
    responses = [actor, member("administrator", bot_permissions, bot_permissions), target or member("member")]
    bot = SimpleNamespace(id=123, get_chat_member=AsyncMock(side_effect=responses), ban_chat_member=AsyncMock())
    message = Mock(
        spec=Message,
        chat=SimpleNamespace(id=-100),
        message_id=50,
        bot=bot,
        html_text="Review",
        edit_text=AsyncMock(),
        delete=AsyncMock(),
        reply_to_message=None,
    )
    query = SimpleNamespace(message=message, from_user=User(id=10, is_bot=False, first_name="Friend"), answer=AsyncMock())
    data = AntiBotCallback(action=action, chat_id=-100, user_id=20)
    deletions = SimpleNamespace(mark_message_to_delete=AsyncMock())
    return query, data, bot, deletions


@pytest.mark.parametrize("actor", [member("member"), member("administrator")])
async def test_limited_actor_cannot_ban(actor):
    query, data, bot, deletions = callback(actor)
    await AntiBot.process_cb(query, data, bot, deletions)
    bot.ban_chat_member.assert_not_awaited()
    assert bot.get_chat_member.await_count == 1
    query.answer.assert_awaited_once()


@pytest.mark.parametrize("status", ["administrator", "creator", "left", "kicked", "restricted"])
async def test_target_membership_is_rechecked(status):
    query, data, bot, deletions = callback(member("creator"), member(status))
    await AntiBot.process_cb(query, data, bot, deletions)
    bot.ban_chat_member.assert_not_awaited()
    query.answer.assert_awaited_once()


async def test_simultaneous_decisions_only_ban_once():
    query, data, bot, deletions = callback(member("creator"))
    bot.get_chat_member.side_effect = [member("creator"), member("administrator", True, True), member("member"), member("creator")]
    await asyncio.gather(AntiBot.process_cb(query, data, bot, deletions), AntiBot.process_cb(query, data, bot, deletions))
    bot.ban_chat_member.assert_awaited_once_with(-100, 20, until_date=None)
    assert query.answer.await_count == 2


@pytest.mark.parametrize("change", [{"action": "unknown"}, {"chat_id": 123}, {"user_id": 0}])
async def test_invalid_callback_cannot_moderate(change):
    query, data, bot, deletions = callback(member("creator"))
    data = data.model_copy(update=change)
    await AntiBot.process_cb(query, data, bot, deletions)
    bot.get_chat_member.assert_not_awaited()
    bot.ban_chat_member.assert_not_awaited()
    query.answer.assert_awaited_once()


def test_non_numeric_callback_is_rejected_before_dispatch():
    with pytest.raises(ValidationError):
        AntiBotCallback.unpack("antibot:ban:-100:invalid")


async def test_bot_permissions_still_required():
    query, data, bot, deletions = callback(member("creator"), bot_permissions=False)
    await AntiBot.process_cb(query, data, bot, deletions)
    bot.ban_chat_member.assert_not_awaited()


def joined(bot, reply=None):
    newcomer = User(id=20, is_bot=False, first_name="iHerb deals")
    message = Mock(
        spec=Message,
        chat=SimpleNamespace(id=-100),
        new_chat_members=[newcomer],
        reply=reply or AsyncMock(),
    )
    return message, SimpleNamespace(mark_message_to_delete=AsyncMock())


@pytest.fixture
def no_moderators():
    AntiBot.moderators.clear()
    yield
    AntiBot.moderators.clear()


async def test_join_check_is_skipped_without_moderation_rights(no_moderators):
    bot = SimpleNamespace(id=123, get_chat_member=AsyncMock(return_value=member("member")))
    message, deletions = joined(bot)
    assert await AntiBot.process(message, bot, deletions) is True
    assert await AntiBot.process(message, bot, deletions) is True
    assert bot.get_chat_member.await_count == 1
    message.reply.assert_not_awaited()


async def test_join_warning_is_best_effort(no_moderators):
    error = TelegramBadRequest(SendMessage(chat_id=1, text="test"), "Bad Request: message to be replied not found")
    bot = SimpleNamespace(id=123, get_chat_member=AsyncMock(side_effect=[member("administrator", True, True), member("member")]))
    message, deletions = joined(bot, reply=AsyncMock(side_effect=error))
    assert await AntiBot.process(message, bot, deletions) is True
    message.reply.assert_awaited_once()
    deletions.mark_message_to_delete.assert_not_awaited()
