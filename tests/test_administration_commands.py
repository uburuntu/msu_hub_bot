import html
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import AnswerInlineQuery, ForwardMessage, GetChatMember
from aiogram.types import Chat, ChatMemberAdministrator, ChatMemberMember, ChatMemberOwner, InlineQuery, Message, User

from hub_bot.commands import admin, debug, infra


def user(user_id=10):
    return User(id=user_id, is_bot=user_id == 123456, first_name="Synthetic")


def message(text="test", **values):
    return Message(
        message_id=values.pop("message_id", 1),
        date=1_700_000_000,
        chat=values.pop("chat", Chat(id=-10012345, type="supergroup", title="Synthetic", username="synthetic")),
        from_user=values.pop("from_user", user()),
        text=text,
        **values,
    )


def administrator(user_id=123456, **permissions):
    defaults = {name: False for name, field in ChatMemberAdministrator.model_fields.items() if field.is_required() and name != "user"}
    defaults.update(permissions)
    return ChatMemberAdministrator(user=user(user_id), **defaults)


@pytest.fixture
def replies(monkeypatch):
    reply = AsyncMock(return_value=message("result"))
    monkeypatch.setattr(Message, "reply", reply)
    monkeypatch.setattr(Message, "answer", reply)
    monkeypatch.setattr(Message, "delete", AsyncMock(return_value=True))
    return reply


@pytest.mark.parametrize(
    "action,method", [("ban", "ban_chat_member"), ("restrict", "restrict_chat_member"), ("unban", "unban_chat_member")]
)
async def test_bulk_admin_uses_native_operations_and_existing_restriction_semantics(monkeypatch, action, method):
    query = SimpleNamespace(list_directory=AsyncMock(return_value=[SimpleNamespace(chat_id=-1), SimpleNamespace(chat_id=-2)]))
    call = AsyncMock(return_value=True)
    bot = SimpleNamespace(**{method: call})
    handler = getattr(admin, f"process_{action}")
    assert await handler(message(f"/{action} 42"), bot, query) == [True, True]
    assert [entry.args[:2] for entry in call.await_args_list] == [(-1, 42), (-2, 42)]
    if action == "restrict":
        assert call.await_args_list[0].args[2].model_dump(exclude_none=True) == {}
    if action == "unban":
        assert all(entry.kwargs == {"only_if_banned": True} for entry in call.await_args_list)
    query.list_directory.reset_mock()
    assert await handler(message(f"/{action} invalid"), bot, query) is None
    query.list_directory.assert_not_awaited()


@pytest.mark.parametrize("promote,already_admin,expected", [(False, False, False), (True, True, False), (True, False, True)])
async def test_sudo_checks_bot_and_target_admin_permissions(replies, promote, already_admin, expected):
    admins = [administrator(can_delete_messages=True, can_promote_members=promote)]
    if already_admin:
        admins.append(administrator(10))
    bot = SimpleNamespace(
        id=123456, get_chat_administrators=AsyncMock(return_value=admins), promote_chat_member=AsyncMock(return_value=True)
    )
    await admin.process_sudo(message("/sudo"), bot, 0)
    assert bot.promote_chat_member.await_count == int(expected)
    if expected:
        assert bot.promote_chat_member.call_args.kwargs["can_promote_members"] is False
        assert bot.promote_chat_member.call_args.args == (-10012345, 10)


@pytest.mark.parametrize("target", ["owner", "admin", "member"])
async def test_revoke_keeps_owner_and_non_admin_guard(replies, target):
    admins = [administrator(can_promote_members=True)]
    if target == "owner":
        admins.append(ChatMemberOwner(user=user(), is_anonymous=False))
    if target == "admin":
        admins.append(administrator(10))
    bot = SimpleNamespace(
        id=123456, get_chat_administrators=AsyncMock(return_value=admins), promote_chat_member=AsyncMock(return_value=True)
    )
    await admin.process_revoke(message("/revoke"), bot, 0)
    assert bot.promote_chat_member.await_count == int(target == "admin")


async def test_admin_without_actor_does_not_promote():
    bot = SimpleNamespace(get_chat_administrators=AsyncMock(), promote_chat_member=AsyncMock())
    assert await admin.process_sudo(message("/sudo", from_user=None), bot, 0) is None
    assert await admin.process_revoke(message("/revoke", from_user=None), bot, 0) is None
    bot.get_chat_administrators.assert_not_awaited()


async def test_forward_range_continues_after_missing_message():
    error = TelegramBadRequest(method=ForwardMessage(chat_id=1, from_chat_id=2, message_id=3), message="missing")
    bot = SimpleNamespace(forward_message=AsyncMock(side_effect=[None, error, None]))
    await admin.process_forwards(message("/forwards -100777 9 3"), bot)
    assert [entry.args[2] for entry in bot.forward_message.await_args_list] == [9, 10, 11]


@pytest.mark.parametrize("promote", [False, True])
async def test_directory_insert_requires_bot_promote_rights(monkeypatch, replies, promote):
    query = SimpleNamespace(get_directory=AsyncMock(return_value=None), create_directory=AsyncMock())
    bot = SimpleNamespace(
        id=123456,
        get_chat_administrators=AsyncMock(return_value=[administrator(can_promote_members=promote)]),
        get_chat_member_count=AsyncMock(return_value=55),
        send_message=AsyncMock(),
    )
    manager = SimpleNamespace(invalidate_directory=lambda: None)
    await infra.process_create_infra_chat(message(), bot, query, 0, manager)
    assert query.create_directory.await_count == int(promote)
    if promote:
        assert query.create_directory.call_args.args[0].members == 55
        assert query.create_directory.call_args.args[0].section == "other"
    bot.send_message.assert_not_awaited()


async def test_directory_delete_retains_existing_owner_authorized_behavior(monkeypatch, replies):
    query = SimpleNamespace(get_directory=AsyncMock(return_value=object()), delete_directory=AsyncMock())
    bot = SimpleNamespace(id=123456, get_chat_administrators=AsyncMock(return_value=[]), send_message=AsyncMock())
    manager = SimpleNamespace(invalidate_directory=lambda: None)
    await infra.process_delete_infra_chat(message(), bot, query, 0, manager)
    query.delete_directory.assert_awaited_once_with(-10012345)


async def test_status_uses_native_member_variants_and_bad_request_marker(monkeypatch, replies):
    chats = {number: SimpleNamespace(chat_id=number, name=f"Chat {number}") for number in range(1, 5)}
    db = SimpleNamespace(list_directory=AsyncMock(return_value=list(chats.values())))
    responses = {
        1: ChatMemberOwner(user=user(123456), is_anonymous=False),
        2: administrator(can_promote_members=False),
        3: ChatMemberMember(user=user(123456)),
    }

    async def get_member(chat_id, user_id):
        assert user_id == 123456
        if chat_id == 4:
            raise TelegramBadRequest(method=GetChatMember(chat_id=chat_id, user_id=user_id), message="missing")
        return responses[chat_id]

    await infra.process_status(message(), SimpleNamespace(id=123456, get_chat_member=get_member), db)
    text = replies.call_args.args[0]
    assert "Chat 1: ✅ ✅" in text and "Chat 2: ✅ ❌" in text
    assert "Chat 3: ❌ ❌" in text and "Chat 4: 💔 💔" in text


async def test_inline_directory_uses_valid_nested_content_and_handles_expired_query(monkeypatch):
    query = InlineQuery(id="synthetic", from_user=user(), query="", offset="")
    answer = AsyncMock(return_value=True)
    monkeypatch.setattr(InlineQuery, "answer", answer)
    em = SimpleNamespace(text=AsyncMock(return_value="<b>Links</b>"))
    assert await infra.process_inline(query, em)
    article = answer.call_args.kwargs["results"][0]
    assert article.input_message_content.message_text == "<b>Links</b>"
    assert answer.call_args.kwargs["cache_time"] == 600
    answer.side_effect = TelegramBadRequest(method=AnswerInlineQuery(inline_query_id="synthetic", results=[]), message="expired")
    assert await infra.process_inline(query, em)


async def test_debug_json_preserves_telegram_aliases_dates_and_redacts_tokens(replies):
    canary = "123456:" + "Z" * 35
    source = message("Synthetic " + canary, message_id=2)
    await debug.process_json(message("/json", reply_to_message=source))
    serialized = html.unescape(replies.call_args.args[0].removeprefix("<pre>").removesuffix("</pre>"))
    payload = json.loads(serialized)
    assert payload["date"] == 1_700_000_000
    assert payload["from"]["id"] == 10
    assert "from_user" not in payload
    assert canary not in serialized and "[REDACTED]" in serialized
    assert replies.call_args.kwargs["disable_notification"] is True


@pytest.mark.parametrize("argument,seconds", [("", 0), ("invalid", 0), ("1", 3), ("999999999", 864000)])
async def test_delete_after_retains_existing_bounds(argument, seconds):
    source = message("source", message_id=2)
    redis = SimpleNamespace(mark_message_to_delete=AsyncMock(return_value=True))
    assert await debug.process_delete_after(message("/delete_after " + argument, reply_to_message=source), redis)
    redis.mark_message_to_delete.assert_awaited_once_with(source, after=seconds)
