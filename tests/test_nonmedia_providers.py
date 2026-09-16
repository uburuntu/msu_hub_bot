"""Provider/game ports keep callback protocols and delivery ownership offline."""

import asyncio
import importlib
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.types import BufferedInputFile, CallbackQuery

from common.tg.runtime import Supervisor
from hub_bot.commands import crypto, geoguess, stats, weather
from telegram_helpers import RecordingSession, make_message


@pytest.mark.parametrize(
    "family,wire,fields",
    [
        ("antibot.AntiBot", "antibot:ban:-100:7", {"action": "ban", "chat_id": -100, "user_id": 7}),
        ("crypto.Crypto", "crypto:BTC", {"ticker": "BTC"}),
        ("dvach.Dvach", "thread_b_update", {"board": "b", "url": "update"}),
        ("geoguess.Geoguess", "geoguess:token:finish", {"round": "token", "choice": "finish"}),
        ("minecraft.MinecraftStatus", "minecraft$example.org:25565", {"url": "example.org:25565"}),
        ("rolls.Randoms", "random:1:100:3", {"begin": 1, "end": 100, "count": 3}),
        ("rolls.Rolls", "roll:3:1", {"digits": 3, "count": 1}),
        ("stats.Stats", "stats:update", {"action": "update"}),
        ("tyan.Tyan", "tyan:sfw:neko", {"type": "sfw", "category": "neko"}),
        ("weather.Weather", "weather:55.75:37.61", {"lat": 55.75, "lon": 37.61}),
        ("weather.WeatherMap", "map:update:55.75:37.61:13", {"action": "update", "lat": 55.75, "lon": 37.61, "zoom": 13}),
    ],
)
def test_callback_wires_remain_compatible(family, wire, fields):
    module, cls = family.split(".")
    callback = getattr(importlib.import_module(f"hub_bot.commands.{module}"), cls).callback_data
    assert callback(**fields).pack() == wire
    assert callback.unpack(wire).model_dump() == fields


@pytest.mark.parametrize(
    "family,fields,services",
    [
        ("antibot.AntiBot", {"action": "ban", "chat_id": -100, "user_id": 7}, {"bot": None, "redis": None}),
        ("crypto.Crypto", {"ticker": "BTC"}, {"crypto_exchange": None}),
        ("dvach.Dvach", {"board": "b", "url": "update"}, {"dvach": None, "bot": None}),
        ("geoguess.Geoguess", {"round": "token", "choice": "finish"}, {"redis": None, "supervisor": None}),
        ("minecraft.MinecraftStatus", {"url": "example.org"}, {}),
        ("rolls.Randoms", {"begin": 1, "end": 100, "count": 3}, {}),
        ("rolls.Rolls", {"digits": 3, "count": 1}, {}),
        ("stats.Stats", None, {"db": None}),
        ("tyan.Tyan", {"type": "sfw", "category": "neko"}, {"settings": None}),
        ("weather.Weather", {"lat": 55.75, "lon": 37.61}, {}),
        ("weather.WeatherMap", {"action": "update", "lat": 55.75, "lon": 37.61, "zoom": 13}, {}),
    ],
)
@pytest.mark.parametrize("inaccessible", [False, True])
async def test_unavailable_message_does_not_touch_injected_services(family, fields, services, inaccessible):
    session = RecordingSession()
    bot = Bot("123456789:" + "a" * 35, session=session)
    values = {"id": "test", "chat_instance": "test", "from_user": {"id": 42, "is_bot": False, "first_name": "User"}}
    if inaccessible:
        values["message"] = {"date": 0, "chat": {"id": -100, "type": "supergroup"}, "message_id": 7}
    query = CallbackQuery.model_validate(values, context={"bot": bot})
    module, cls = family.split(".")
    command = getattr(importlib.import_module(f"hub_bot.commands.{module}"), cls)
    kwargs = dict(services)
    if fields:
        kwargs["callback_data"] = command.callback_data(**fields)
    await command.process_cb(query, **kwargs)
    assert [method.__api_method__ for method in session.methods] == ["answerCallbackQuery"]


async def test_crypto_uses_injected_exchange_and_preserves_three_price_lookups():
    exchange = SimpleNamespace(fetch_ohlcv=AsyncMock(side_effect=[[[0, 0, 0, 0, 100]], [[0, 0, 0, 0, 80]], [[0, 0, 0, 0, 50]]]))
    text = await crypto.Crypto.text.__wrapped__(crypto.Crypto, "BTC", exchange)
    assert exchange.fetch_ohlcv.await_count == 3
    assert "25.000" in text and "100.000" in text
    assert all(call.args == ("BTC/USDT",) for call in exchange.fetch_ohlcv.await_args_list)


@pytest.mark.parametrize("ticker", ["BTC", "ETH"])
async def test_crypto_failure_waits_for_other_price_requests(ticker):
    entered, release = asyncio.Event(), asyncio.Event()
    original = RuntimeError("synthetic price failure")

    async def fetch(symbol, **kwargs):
        if (ticker == "BTC" and "since" not in kwargs) or (ticker == "ETH" and symbol.endswith("/BTC")):
            raise original
        entered.set()
        await release.wait()
        return [[0, 0, 0, 0, 100]]

    task = asyncio.create_task(crypto.Crypto.text.__wrapped__(crypto.Crypto, ticker, SimpleNamespace(fetch_ohlcv=fetch)))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        done, _ = await asyncio.wait({task}, timeout=0.01)
        assert not done
        release.set()
        with pytest.raises(RuntimeError) as caught:
            await task
        assert caught.value is original
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_stats_failure_waits_for_other_database_queries(monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    original = RuntimeError("synthetic count failure")
    completed = []

    async def count(*args):
        entered.set()
        await release.wait()
        completed.append(True)
        return 1

    monkeypatch.setattr(stats.UserDB, "query", lambda db: SimpleNamespace(count=AsyncMock(side_effect=original)))
    for model in (stats.ChatDB, stats.UpdateDB):
        monkeypatch.setattr(model, "query", lambda db: SimpleNamespace(count=count))
    task = asyncio.create_task(stats.Stats.text(SimpleNamespace()))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        done, _ = await asyncio.wait({task}, timeout=0.01)
        assert not done
        release.set()
        with pytest.raises(RuntimeError) as caught:
            await task
        assert caught.value is original and len(completed) == 3
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_weather_map_snapshots_upload_and_keeps_topic(monkeypatch):
    session = RecordingSession()
    bot = Bot("123456789:" + "a" * 35, session=session)
    message = make_message(bot, message_thread_id=17, is_topic_message=True)
    source = io.BytesIO(b"synthetic image")
    source.seek(5)
    weather.WeatherMap.file_ids.clear()
    monkeypatch.setattr(weather, "weather_map", AsyncMock(return_value=source))
    await weather.WeatherMap.process(message)
    sent = session.methods[-1]
    assert isinstance(sent.photo, BufferedInputFile)
    assert sent.photo.data == b"synthetic image" and sent.photo.filename == "weather-map.png"
    assert not source.closed and source.tell() == 5
    assert sent.message_thread_id == 17
    assert sent.reply_parameters.message_id == message.message_id


async def test_geoguess_timeout_reaches_transport():
    class Session(RecordingSession):
        async def make_request(self, bot, method, timeout=None):
            self.timeout = timeout
            return await super().make_request(bot, method, timeout)

    session = Session()
    bot = Bot("123456789:" + "a" * 35, session=session)
    await geoguess._send(make_message(bot).reply("Synthetic"))
    assert session.timeout == geoguess.SEND_TIMEOUT
    assert "request_timeout" not in session.methods[-1].model_extra


async def test_geoguess_finish_is_owned_by_supervisor(monkeypatch):
    session = RecordingSession()
    bot = Bot("123456789:" + "a" * 35, session=session)
    message = make_message(bot)
    round_ = geoguess.Round(token="test", message=message)
    geoguess.Geoguess.rounds[message.chat.id] = round_
    supervisor = Supervisor()
    entered, release = asyncio.Event(), asyncio.Event()

    async def finish(*args):
        entered.set()
        await release.wait()

    monkeypatch.setattr(geoguess.Geoguess, "finish", finish)
    query = CallbackQuery.model_validate(
        {"id": "test", "chat_instance": "test", "from_user": {"id": 42, "is_bot": False, "first_name": "User"}, "message": message},
        context={"bot": bot},
    )
    worker = asyncio.create_task(
        geoguess.Geoguess.process_cb(query, geoguess.GeoguessCallback(round="test", choice="finish"), None, supervisor)
    )
    await entered.wait()
    assert supervisor.job_count == 1 and round_.closed
    release.set()
    await worker
    await supervisor.drain()
    assert supervisor.job_count == 0
    geoguess.Geoguess.rounds.clear()
