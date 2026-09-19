"""VK domain variants reach the same public post adapter and Telegram destination."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Dispatcher
from aiogram.methods import SendMessage
from aiogram.types import Update

from msu_hub_bot.community.reposts import RepostError, Reposts
from msu_hub_bot.providers.vk.api import VkApi
from msu_hub_bot.storage.features import FeatureStore
from msu_hub_bot.telegram.middlewares.settings import Settings
from msu_hub_bot.telegram.middlewares.viewer import ViewerMiddleware
from msu_hub_bot.telegram.wrapper import BotWrapper
from quiz_helpers import FeatureFixture
from telegram_helpers import RecordingSession, make_message
from test_dispatch_contract import router

URLS = (
    [
        f"{scheme}{prefix}vk.{domain}/wall-10_6"
        for domain in ("com", "ru")
        for prefix in ("", "www.", "m.")
        for scheme in ("", "http://", "https://")
    ]
    + [f"https://vk.{domain}/{page}?w=wall-10_6" for domain in ("com", "ru") for page in ("news", "example.page", "example-page")]
    + ["https://WWW.VK.RU/wall-10_6"]
)


@pytest.fixture
async def runtime():
    session = RecordingSession()
    bot = BotWrapper("123456789:" + "a" * 35, session=session)
    api = VkApi("synthetic-token")

    async def request(method, **params):
        if method == "groups.getById":
            assert params == {"group_ids": "10"}
            return {"groups": [{"id": 10, "is_closed": 0}]}
        assert method == "wall.getById"
        assert params["posts"] == "-10_6"
        return {"items": [{"id": 6, "owner_id": -10, "text": "Synthetic public post"}]}

    api.request = AsyncMock(side_effect=request)
    yield bot, api, session
    await api.close()
    await session.close()


@pytest.mark.parametrize("url", URLS)
@pytest.mark.parametrize("caption", [False, True], ids=["text", "caption"])
async def test_automatic_post_preview_accepts_both_domains_after_real_dispatch(runtime, url, caption):
    bot, api, session = runtime
    dispatcher = Dispatcher()
    dispatcher.message.outer_middleware(ViewerMiddleware(bot, api, SimpleNamespace(run=AsyncMock())))

    @dispatcher.message()
    async def received(message):
        return None

    text = "📎 " + url
    entity = {"type": "url", "offset": 3, "length": len(url)}
    fields = (
        {"caption": text, "caption_entities": [entity], "photo": [{"file_id": "photo", "file_unique_id": "one", "width": 1, "height": 1}]}
        if caption
        else {"text": text, "entities": [entity]}
    )
    message = make_message(bot, message_id=501, **fields)
    await dispatcher.feed_update(bot, Update(update_id=1, message=message), settings=Settings(auto_video_links=False))
    api.request.assert_any_await("wall.getById", posts="-10_6", extended=1, copy_history_depth=2)
    sent = [method for method in session.methods if isinstance(method, SendMessage)]
    assert len(sent) == 1 and "Synthetic public post" in sent[0].text
    assert sent[0].chat_id == message.chat.id and sent[0].reply_parameters.message_id == 501


@pytest.mark.parametrize("url", URLS)
async def test_explicit_vk_post_routes_both_domains_to_requested_chat(runtime, url):
    bot, api, session = runtime
    dispatcher = Dispatcher()
    dispatcher.include_router(router())
    message = make_message(
        bot,
        text=f"/vk_post {url} -20 0",
        from_user={"id": 7, "is_bot": False, "first_name": "Owner"},
    )
    await dispatcher.feed_update(bot, Update(update_id=1, message=message), vk_api=api)
    api.request.assert_any_await("wall.getById", posts="-10_6", extended=1, copy_history_depth=2)
    sent = [method for method in session.methods if isinstance(method, SendMessage)]
    assert len(sent) == 1 and sent[0].chat_id == -20 and sent[0].text == "Synthetic public post"


@pytest.mark.parametrize("host", ["vk.com", "www.vk.com", "m.vk.com", "vk.ru", "www.vk.ru", "m.vk.ru"])
@pytest.mark.parametrize("scheme", ["", "https://"])
async def test_source_configuration_accepts_mobile_and_www_domains(host, scheme):
    service = Reposts(FeatureStore(FeatureFixture()))
    assert await service.resolve(f"{scheme}{host}/wall-10_6") == -10
    assert await service.resolve(f"{scheme}{host}/club10") == -10


@pytest.mark.parametrize(
    "url", ["https://vk.ru.evil.test/wall-10_6", "https://evil.test/vk.ru/wall-10_6", "https://vk.ru@evil.test/wall-10_6"]
)
async def test_lookalike_domains_never_reach_the_post_adapter(runtime, url):
    bot, api, session = runtime
    message = make_message(bot, text=url, entities=[{"type": "url", "offset": 0, "length": len(url)}])
    await ViewerMiddleware(bot, api, SimpleNamespace()).view(message, Settings(auto_video_links=False))
    api.request.assert_not_awaited()
    assert not session.methods
    with pytest.raises(RepostError):
        await Reposts(FeatureStore(FeatureFixture())).resolve(url)


async def test_automatic_preview_does_not_reprocess_a_replied_to_old_link(runtime):
    bot, api, session = runtime
    url = "https://vk.ru/wall-10_6"
    old = make_message(bot, text=url, entities=[{"type": "url", "offset": 0, "length": len(url)}])
    current = make_message(bot, message_id=502, text="Reply without a new link", reply_to_message=old)
    await ViewerMiddleware(bot, api, SimpleNamespace()).view(current, Settings(auto_video_links=False))
    api.request.assert_not_awaited()
    assert not session.methods
