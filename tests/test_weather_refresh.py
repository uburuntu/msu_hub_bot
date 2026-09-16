import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def weather_module(monkeypatch):
    monkeypatch.setitem(sys.modules, 'app', SimpleNamespace(bot=SimpleNamespace(get_chat=AsyncMock())))
    spec = importlib.util.spec_from_file_location('weather_refresh_test', Path(__file__).resolve().parents[1] / 'hub_bot/commands/weather.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.weather = AsyncMock(return_value=('forecast', 'place'))
    module.parse_response = lambda *args: 'synthetic forecast'
    module.clock = 0
    module.time = SimpleNamespace(monotonic=lambda: module.clock)
    return module


def message(*, chat=1, latitude=10):
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat), message_id=2,
        location=SimpleNamespace(latitude=latitude, longitude=20), venue=None,
        reply=AsyncMock(return_value=SimpleNamespace(message_id=3)),
        bot=SimpleNamespace(edit_message_text=AsyncMock()),
    )


async def test_live_location_waits_fifteen_minutes_and_uses_newest_eligible_position(weather_module):
    module = weather_module
    original = message()
    await module.Weather.process_location(original)
    for now in (1, 30, 899.99):
        module.clock = now
        await module.Weather.process_location_edited(message(latitude=11))
    assert module.weather.await_count == 1

    module.clock = 900
    newest = message(latitude=12)
    await module.Weather.process_location_edited(newest)
    module.weather.assert_awaited_with((12, 20), None)
    newest.bot.edit_message_text.assert_awaited_once()
    assert module.weather.await_count == 2
    module.clock = 901
    await module.Weather.process_location_edited(message(latitude=13))
    assert module.weather.await_count == 2


async def test_orphan_location_edit_does_not_call_provider(weather_module):
    assert await weather_module.Weather.process_location_edited(message()) is True
    weather_module.weather.assert_not_awaited()


async def test_location_refresh_is_independent_per_chat(weather_module):
    first, second = message(chat=1), message(chat=2)
    await weather_module.Weather.process_location(first)
    weather_module.clock = 500
    await weather_module.Weather.process_location(second)
    weather_module.clock = 900
    await weather_module.Weather.process_location_edited(first)
    await weather_module.Weather.process_location_edited(second)
    first.bot.edit_message_text.assert_awaited_once()
    second.bot.edit_message_text.assert_not_awaited()


async def test_concurrent_edits_make_only_one_refresh(weather_module):
    module = weather_module
    original = message()
    await module.Weather.process_location(original)
    module.clock = 900
    started, release = asyncio.Event(), asyncio.Event()

    async def provider(*args):
        started.set()
        await release.wait()
        return 'forecast', 'place'

    module.weather.side_effect = provider
    first = asyncio.create_task(module.Weather.process_location_edited(original))
    await started.wait()
    second = asyncio.create_task(module.Weather.process_location_edited(message(latitude=12)))
    release.set()
    await asyncio.gather(first, second)
    assert module.weather.await_count == 2  # Initial reply plus one eligible edit.


async def test_failed_refresh_does_not_retry_on_every_location_edit(weather_module):
    module = weather_module
    original = message()
    await module.Weather.process_location(original)
    module.weather.return_value = None
    module.clock = 900
    await module.Weather.process_location_edited(original)
    module.clock = 901
    await module.Weather.process_location_edited(original)
    assert module.weather.await_count == 2
    original.bot.edit_message_text.assert_not_awaited()


async def test_explicit_refresh_is_not_limited_by_live_location_timer(weather_module):
    module = weather_module
    await module.Weather.process_location(message())
    module.clock = 1
    query = SimpleNamespace(answer=AsyncMock(), message=SimpleNamespace(edit_text=AsyncMock()))
    await module.Weather.process_cb(query, {'lat': '10', 'lon': '20'})
    query.answer.assert_awaited_once()
    query.message.edit_text.assert_awaited_once()
    assert module.weather.await_count == 2


@pytest.mark.parametrize('coordinates', [{}, {'lat': 'nan', 'lon': '20'}, {'lat': '91', 'lon': '20'}, {'lat': '10', 'lon': 'broken'}])
async def test_bad_callback_coordinates_are_acknowledged_without_provider_calls(weather_module, coordinates):
    query = SimpleNamespace(answer=AsyncMock(), message=SimpleNamespace(edit_text=AsyncMock()))
    await weather_module.Weather.process_cb(query, coordinates)
    assert query.answer.call_args.kwargs['show_alert'] is True
    weather_module.weather.assert_not_awaited()


async def test_unavailable_callback_message_is_acknowledged(weather_module):
    query = SimpleNamespace(answer=AsyncMock(), message=None)
    await weather_module.Weather.process_cb(query, {'lat': '10', 'lon': '20'})
    assert query.answer.call_args.kwargs['show_alert'] is True
    weather_module.weather.assert_not_awaited()
