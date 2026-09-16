import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import ChatMember


@pytest.fixture
def moderation():
    source = Path("hub_bot/commands/antibot.py").read_text()
    tree = ast.parse(source)
    tree.body = [node for node in tree.body if not (isinstance(node, ast.ImportFrom) and node.module == "app")]
    namespace = {"redis": SimpleNamespace(mark_message_to_delete=AsyncMock())}
    exec(compile(tree, "<antibot>", "exec"), namespace)
    return namespace["AntiBot"]


def member(status, can_restrict=False, can_delete=False):
    return ChatMember.to_object(
        dict(
            status=status,
            user=dict(id=1, is_bot=False, first_name="Friend"),
            can_restrict_members=can_restrict,
            can_delete_messages=can_delete,
        )
    )


def callback(actor, target=None, action="ban", bot_permissions=True):
    responses = [actor, member("administrator", bot_permissions, bot_permissions), target or member("member")]
    bot = SimpleNamespace(id=123, get_chat_member=AsyncMock(side_effect=responses), kick_chat_member=AsyncMock())
    message = SimpleNamespace(
        chat=SimpleNamespace(id=-100),
        message_id=50,
        bot=bot,
        html_text="Review",
        edit_text=AsyncMock(),
        delete=AsyncMock(),
        reply_to_message=None,
    )
    query = SimpleNamespace(message=message, from_user=SimpleNamespace(id=10, get_mention=lambda: "Friend"), answer=AsyncMock())
    data = {"action": action, "chat_id": "-100", "user_id": "20"}
    return query, data, bot


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", [member("member"), member("administrator")])
async def test_limited_actor_cannot_ban(moderation, actor):
    query, data, bot = callback(actor)
    await moderation.process_cb(query, data)
    bot.kick_chat_member.assert_not_awaited()
    assert bot.get_chat_member.await_count == 1
    query.answer.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["administrator", "creator", "left", "kicked"])
async def test_target_membership_is_rechecked(moderation, status):
    query, data, bot = callback(member("creator"), member(status))
    await moderation.process_cb(query, data)
    bot.kick_chat_member.assert_not_awaited()
    query.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_simultaneous_decisions_only_ban_once(moderation):
    query, data, bot = callback(member("creator"))
    bot.get_chat_member.side_effect = [member("creator"), member("administrator", True, True), member("member"), member("creator")]
    await asyncio.gather(moderation.process_cb(query, data), moderation.process_cb(query, data))
    bot.kick_chat_member.assert_awaited_once_with(-100, 20, until_date=None)
    assert query.answer.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [{"action": "unknown"}, {"chat_id": "123"}, {"user_id": "invalid"}])
async def test_invalid_callback_cannot_moderate(moderation, change):
    query, data, bot = callback(member("creator"))
    data.update(change)
    await moderation.process_cb(query, data)
    bot.get_chat_member.assert_not_awaited()
    bot.kick_chat_member.assert_not_awaited()
    query.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_bot_permissions_still_required(moderation):
    query, data, bot = callback(member("creator"), bot_permissions=False)
    await moderation.process_cb(query, data)
    bot.kick_chat_member.assert_not_awaited()
