import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from aiogram.types import Chat, Message, User
from pendulum import from_timestamp

from hub_bot.commands import vk
from hub_bot.db import VkWallPosting


def message(text):
    return Message(
        message_id=1,
        date=1_700_000_000,
        chat=Chat(id=12345, type="private"),
        from_user=User(id=10, is_bot=False, first_name="Synthetic"),
        text=text,
    )


def config(**values):
    defaults = dict(id=UUID(int=1), owner_id=-10, chat_id=-20, last_post_id=5, with_reposts=False, with_header=True, is_suspended=False)
    defaults.update(values)
    return VkWallPosting(**defaults)


def post(number=6, **values):
    return SimpleNamespace(id=number, owner_id=values.get("owner_id", -10), is_repost=values.get("is_repost", False), date=number)


@pytest.fixture
def replies(monkeypatch):
    reply = AsyncMock(return_value=message("result"))
    monkeypatch.setattr(Message, "reply", reply)
    return reply


async def test_vk_push_skips_old_posts_and_reposts_then_marks_delivered_post(monkeypatch):
    order = []

    async def publish(*args, **kwargs):
        order.append("publish")

    async def update(*args, **kwargs):
        order.append("marker")

    publisher = AsyncMock(side_effect=publish)
    query = SimpleNamespace(update2=AsyncMock(side_effect=update))
    monkeypatch.setattr(vk, "publish_vk_post", publisher)
    monkeypatch.setattr(VkWallPosting, "query", lambda db: query)
    bot = SimpleNamespace(send_message=AsyncMock())
    newest = post(7)
    await vk._push_posts([post(4), post(6, is_repost=True), newest], -20, {(-10, -20): config()}, 12345, bot, object())
    assert order == ["publish", "marker"]
    publisher.assert_awaited_once_with(newest, bot, -20, with_header=True)
    query.update2.assert_awaited_once_with("owner_id", -10, "chat_id", -20, last_post_id=7)
    bot.send_message.assert_not_awaited()


async def test_vk_push_failure_keeps_marker_and_never_sends_raw_error(monkeypatch, capsys):
    publisher = AsyncMock(side_effect=RuntimeError("private-provider-canary"))
    query = SimpleNamespace(update2=AsyncMock())
    monkeypatch.setattr(vk, "publish_vk_post", publisher)
    monkeypatch.setattr(VkWallPosting, "query", lambda db: query)
    bot = SimpleNamespace(send_message=AsyncMock())
    await vk._push_posts([post()], -20, {(-10, -20): config()}, 12345, bot, object())
    query.update2.assert_not_awaited()
    bot.send_message.assert_awaited_once()
    assert "private-provider-canary" not in repr(bot.send_message.call_args) + capsys.readouterr().out
    assert "Traceback" not in repr(bot.send_message.call_args)


async def test_vk_push_continues_existing_best_effort_batch_after_one_failure(monkeypatch):
    publisher = AsyncMock(side_effect=[RuntimeError("synthetic"), None])
    query = SimpleNamespace(update2=AsyncMock())
    monkeypatch.setattr(vk, "publish_vk_post", publisher)
    monkeypatch.setattr(VkWallPosting, "query", lambda db: query)
    bot = SimpleNamespace(send_message=AsyncMock())
    await vk._push_posts([post(6), post(7)], -20, {(-10, -20): config()}, 0, bot, object())
    query.update2.assert_awaited_once_with("owner_id", -10, "chat_id", -20, last_post_id=7)
    bot.send_message.assert_not_awaited()


async def test_vk_wall_suspend_updates_configuration_without_provider_request(monkeypatch, replies):
    query = SimpleNamespace(upsert2=AsyncMock(return_value=config(is_suspended=True)))
    monkeypatch.setattr(VkWallPosting, "query", lambda db: query)
    provider = AsyncMock()
    monkeypatch.setattr(vk.VkPost, "from_api_wall", provider)
    await vk.process_vk_wall(message("/vk_wall -10 -20 5 0 1 1 pause"), SimpleNamespace(), object(), object())
    assert query.upsert2.call_args.kwargs == dict(
        owner_id=-10,
        chat_id=-20,
        last_post_id=5,
        with_reposts=False,
        with_header=True,
        is_suspended=True,
        description="pause",
    )
    provider.assert_not_awaited()
    assert "заморожена" in replies.call_args.args[0]


async def test_vk_wall_uses_injected_api_bot_and_database(monkeypatch, replies):
    entry = config()
    query = SimpleNamespace(upsert2=AsyncMock(return_value=entry))
    monkeypatch.setattr(VkWallPosting, "query", lambda db: query)
    posts = [post()]
    provider = AsyncMock(return_value=posts)
    push = AsyncMock()
    monkeypatch.setattr(vk.VkPost, "from_api_wall", provider)
    monkeypatch.setattr(vk, "_push_posts", push)
    bot, db, api = object(), object(), object()
    await vk.process_vk_wall(message("/vk_wall -10 -20"), bot, db, api)
    provider.assert_awaited_once_with(api, -10)
    push.assert_awaited_once_with(posts, -20, {(-10, -20): entry}, 12345, bot, db)
    assert "окончена" in replies.call_args.args[0]


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


@pytest.mark.parametrize("failure", [False, True])
async def test_watermark_uses_existing_default_and_commits_only_clean_exit(failure):
    redis = SimpleNamespace(get_dt=AsyncMock(return_value=None), set_dt=AsyncMock())
    updater = vk.LastCheckUpdater(redis, "vk_newsfeed_last_check")
    if failure:
        with pytest.raises(RuntimeError):
            async with updater as value:
                assert value == updater.curr_dt.subtract(days=10)
                raise RuntimeError("synthetic provider error")
        redis.set_dt.assert_not_awaited()
    else:
        async with updater as value:
            assert value == updater.curr_dt.subtract(days=10)
        redis.set_dt.assert_awaited_once_with("vk_newsfeed_last_check", updater.curr_dt)


async def test_scheduled_vk_batch_keeps_suspension_grouping_order_and_aggregate_logs(monkeypatch, caplog):
    configs = [config(), config(chat_id=-30), config(owner_id=-11, chat_id=-40), config(owner_id=-12, is_suspended=True)]
    monkeypatch.setattr(VkWallPosting, "query", lambda db: SimpleNamespace(get_all=AsyncMock(return_value=configs)))
    earlier, later = post(6), post(7)
    provider = AsyncMock(return_value=[later, earlier])
    push = AsyncMock()
    monkeypatch.setattr(vk.VkPost, "from_api_newsfeed", provider)
    monkeypatch.setattr(vk, "_push_posts", push)
    last = from_timestamp(1_700_000_000)
    redis = SimpleNamespace(get_dt=AsyncMock(return_value=last), set_dt=AsyncMock())
    bot, db, api = object(), object(), object()
    logger = logging.getLogger("tests.vk")
    caplog.set_level("INFO", logger="tests.vk")
    assert await vk.process_vk_wall_posting(bot, db, redis, api, 12345, logger)
    provider.assert_awaited_once_with(api, owner_ids={-10, -11}, from_ts=1_700_000_000)
    assert [call.args[1] for call in push.await_args_list] == [-20, -30]
    assert all(call.args[0] == [earlier, later] for call in push.await_args_list)
    assert "2 post(s) from 1 wall(s) to 2 chat(s)" in caplog.text
    assert "12345" not in caplog.text and "1700000000" not in caplog.text
    redis.set_dt.assert_awaited_once()
