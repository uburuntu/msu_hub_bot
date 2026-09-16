"""Nonmedia commands preserve Telegram payloads across the framework boundary."""

from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from aiogram.types import ChatMemberMember

from common.tg.filters import MetaInfo
from common.tg.middlewares.settings import Settings
from hub_bot.commands import location, other, settings, votes
from telegram_helpers import RecordingSession, make_message


@pytest.fixture
def transport():
    session = RecordingSession()
    return Bot("123456789:" + "a" * 35, session=session), session


async def test_copy_keeps_topic_and_nested_reply(transport):
    bot, session = transport
    message = make_message(bot, message_thread_id=17, is_topic_message=True, reply_to_message=make_message(message_id=8))
    await other.process_copy(message)
    sent = session.methods[-1]
    assert sent.__api_method__ == "copyMessage"
    assert sent.message_id == 8
    assert sent.message_thread_id == 17
    assert sent.reply_parameters.message_id == message.message_id


@pytest.mark.parametrize(
    "origin,name",
    [
        ({"type": "user", "sender_user": {"id": 7, "is_bot": False, "first_name": "Sender"}}, "Sender"),
        ({"type": "hidden_user", "sender_user_name": "Hidden"}, "Hidden"),
        ({"type": "chat", "sender_chat": {"id": -9, "type": "supergroup", "title": "Group"}}, "Group"),
        ({"type": "channel", "chat": {"id": -9, "type": "channel", "title": "Channel"}, "message_id": 3}, "Channel"),
    ],
)
async def test_votes_preserve_forward_attribution_with_v3_origins(transport, origin, name):
    bot, session = transport
    target = make_message(bot, text="Synthetic text", forward_origin={"date": 1, **origin})
    await votes.process_votes(target, MetaInfo(target, text="Synthetic text"))
    sent = session.methods[-1]
    assert sent.question == f"{name}: Synthetic text"
    assert [option.text for option in sent.options] == ["👍🏻", "👎🏻", "🤔"]
    assert sent.is_anonymous is False


async def test_location_keeps_three_links_and_two_rows(transport):
    bot, session = transport
    message = make_message(bot, text="/location 55.75 37.61", message_thread_id=17, is_topic_message=True)
    await location.process_location(message, MetaInfo(message, text="55.75 37.61"))
    sent = session.methods[-1]
    assert (sent.latitude, sent.longitude) == (55.75, 37.61)
    assert [len(row) for row in sent.reply_markup.inline_keyboard] == [2, 1]
    assert sent.message_thread_id == 17


async def test_settings_nonadmin_cannot_mutate_preferences(transport, monkeypatch):
    bot, session = transport
    message = make_message(bot)
    member = ChatMemberMember(status=ChatMemberStatus.MEMBER, user=message.from_user)
    monkeypatch.setattr(bot, "get_chat_member", AsyncMock(return_value=member))
    prefs = Settings()
    await settings.process_settings(message, MetaInfo(message, arguments=["with_nsfw", "true"]), prefs)
    assert prefs.with_nsfw is False
    assert "только админы" in session.methods[-1].text


@pytest.mark.parametrize(
    "family,wire,fields",
    [
        ("debate.Debate", "debate:rules:7", {"action": "rules", "uid": "7"}),
        ("likes.Like", "like:3", {"count": 3}),
        ("rate.Rate", "rate:+", {"is_up": "+"}),
        ("raffle.Raffle", "raffle:reg", {"action": "reg"}),
        ("help.HelpMessage", "help:open", {"action": "open"}),
    ],
)
def test_legacy_callback_wires_remain_compatible(family, wire, fields):
    import importlib

    module, cls = family.split(".")
    callback = getattr(importlib.import_module(f"hub_bot.commands.{module}"), cls).callback_data
    assert callback(**fields).pack() == wire
    assert callback.unpack(wire).model_dump() == fields


@pytest.mark.parametrize("family", ["likes.Like", "rate.Rate", "raffle.Raffle", "help.HelpMessage"])
@pytest.mark.parametrize("inaccessible", [False, True])
async def test_callback_without_accessible_message_is_acknowledged(transport, family, inaccessible):
    import importlib
    from types import SimpleNamespace

    from aiogram.types import CallbackQuery

    bot, session = transport
    values = {
        "id": "synthetic",
        "from_user": {"id": 42, "is_bot": False, "first_name": "User"},
        "chat_instance": "test",
    }
    if inaccessible:
        values["message"] = {"date": 0, "chat": {"id": -1001, "type": "supergroup"}, "message_id": 7}
    query = CallbackQuery.model_validate(values, context={"bot": bot})
    module, cls = family.split(".")
    command = getattr(importlib.import_module(f"hub_bot.commands.{module}"), cls)
    kwargs = {}
    if module == "rate":
        kwargs["callback_data"] = command.callback_data(is_up="+")
    elif module in {"help", "raffle"}:
        kwargs["callback_data"] = command.callback_data(action="open" if module == "help" else "reg")
    if module == "help":
        kwargs["supervisor"] = SimpleNamespace(create_job=lambda factory: pytest.fail("No job may be created"))
    await command.process_cb(query, **kwargs)
    assert [method.__api_method__ for method in session.methods] == ["answerCallbackQuery"]
