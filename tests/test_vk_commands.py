from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from aiogram.types import Chat, Message, User
from pendulum import from_timestamp

from msu_hub_bot.commands import vk
from msu_hub_bot.storage.models import VkSubscription


def message(text):
    return Message(
        message_id=1,
        date=1_700_000_000,
        chat=Chat(id=12345, type="private"),
        from_user=User(id=10, is_bot=False, first_name="Synthetic"),
        text=text,
    )


def config(**values):
    defaults = dict(
        id=UUID(int=1),
        created=from_timestamp(1_700_000_000),
        owner_id=-10,
        chat_id=-20,
        last_post_id=5,
        with_reposts=False,
        with_header=True,
        is_suspended=False,
    )
    defaults.update(values)
    return VkSubscription(**defaults)


def post(number=6, **values):
    return SimpleNamespace(id=number, owner_id=values.get("owner_id", -10), is_repost=values.get("is_repost", False), date=number)


@pytest.fixture
def replies(monkeypatch):
    reply = AsyncMock(return_value=message("result"))
    monkeypatch.setattr(Message, "reply", reply)
    return reply


async def test_vk_wall_suspend_updates_configuration_without_provider_request(monkeypatch, replies):
    query = SimpleNamespace(upsert_vk_subscription=AsyncMock(return_value=config(is_suspended=True)))
    provider = AsyncMock()
    monkeypatch.setattr(vk.VkPost, "from_api_wall", provider)
    await vk.process_vk_wall(message("/vk_wall -10 -20 5 0 1 1 pause"), SimpleNamespace(), query, object())
    assert query.upsert_vk_subscription.call_args.args[:2] == (-10, -20)
    assert query.upsert_vk_subscription.call_args.args[2].model_dump(exclude_unset=True) == dict(
        last_post_id=5,
        with_reposts=False,
        with_header=True,
        is_suspended=True,
        description="pause",
    )
    provider.assert_not_awaited()
    assert "заморожена" in replies.call_args.args[0]


async def test_vk_post_preserves_url_parser_and_header_flag(monkeypatch, replies):
    selected = post()
    provider = AsyncMock(return_value=[selected])
    publisher = AsyncMock()
    monkeypatch.setattr(vk.VkPost, "from_api_by_id", provider)
    monkeypatch.setattr(vk, "publish_vk_post", publisher)
    api, bot = object(), object()
    assert await vk.process_vk_post(message("/vk_post https://vk.com/wall-10_6 -20 0"), bot, api)
    provider.assert_awaited_once_with(api, "-10_6")
    publisher.assert_awaited_once_with(selected, bot, -20, None, False)
