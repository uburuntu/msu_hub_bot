"""OpenWeather contracts use synthetic payloads and never require API keys."""

import datetime
import importlib.util
import io
import sys
import traceback
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest
from PIL import Image

from msu_hub_bot.providers import owm
from msu_hub_bot.settings import MissingIntegration


def payloads():
    condition = [{"id": 800, "main": "Clear", "description": "ясно <небо>", "icon": "01d"}]
    current = {"dt": 1726502400, "main": {"temp": 12.5, "feels_like": 11.2}, "weather": condition, "name": "Synthetic place"}
    forecast = {
        "city": {"timezone": 7200, "name": "Fallback place"},
        "list": [
            {"dt": current["dt"] + index * 10800, "main": {"temp": value, "feels_like": value - 1}, "weather": condition}
            for index, value in enumerate([13.5, 15.2, 12.8, 10.4], 1)
        ],
    }
    return current, forecast


class Response:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload

    async def read(self):
        return self.payload


class Session:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []
        self.options = None
        self.closed = False

    def factory(self, **kwargs):
        self.options = kwargs
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture
def configure(monkeypatch):
    monkeypatch.setattr(owm.settings, "owm_key", "synthetic-api-key")
    monkeypatch.setattr(owm.settings, "mapbox_key", "")

    def configured(responses):
        session = Session(responses)
        monkeypatch.setattr(owm.aiohttp, "ClientSession", session.factory)
        return session

    return configured


async def test_current_and_three_hour_forecast_do_not_require_one_call(configure):
    current, forecast = payloads()
    session = configure([Response(current), Response(forecast)])

    result, name = await owm.weather.__wrapped__((52.52, 13.405))

    assert name == "Synthetic place"
    assert result.current.temp == 12.5
    assert result.current.feels_like == 11.2
    assert len(result.periods) == 4
    assert result.periods[1].dt - result.periods[0].dt == datetime.timedelta(hours=3)
    assert result.timezone_offset == 7200
    assert [url for url, _ in session.calls] == [
        "https://api.openweathermap.org/data/2.5/weather",
        "https://api.openweathermap.org/data/2.5/forecast",
    ]
    assert all(
        kwargs["params"] == {"lat": 52.52, "lon": 13.405, "units": "metric", "lang": "ru", "appid": "synthetic-api-key"}
        for _, kwargs in session.calls
    )
    assert session.options["timeout"].total == 15
    assert session.closed


async def test_cache_keeps_location_names_distinct_and_accepts_default_argument(configure):
    current, forecast = payloads()
    session = configure([Response(current), Response(forecast)] * 3)
    await owm.weather.cache.clear()
    try:
        assert (await owm.weather((52.52, 13.405), "First place"))[1] == "First place"
        assert (await owm.weather((52.52, 13.405), "Second place"))[1] == "Second place"
        assert (await owm.weather((52.52, 13.405)))[1] == "Synthetic place"
        assert (await owm.weather((52.52, 13.405), "First place"))[1] == "First place"
        assert len(session.calls) == 6
    finally:
        await owm.weather.cache.clear()


@pytest.mark.parametrize("status", [400, 401, 404, 429, 503])
async def test_service_status_is_classified_without_exposing_its_body(configure, status):
    session = configure([Response({"message": "synthetic-secret-must-not-appear"}, status=status)])

    with pytest.raises(owm.WeatherServiceError) as caught:
        await owm.weather.__wrapped__((52.52, 13.405))

    assert caught.value.status == status
    assert "synthetic-secret" not in str(caught.value)
    assert session.closed


@pytest.mark.parametrize(
    "failure",
    [
        aiohttp.ClientError("https://example.org/?appid=synthetic-canary"),
        TimeoutError("synthetic-canary"),
        ValueError("synthetic-canary"),
    ],
)
async def test_transport_and_json_failures_hide_sensitive_diagnostics(configure, failure):
    configure([Response(failure) if isinstance(failure, ValueError) else failure])

    with pytest.raises(owm.WeatherServiceError) as caught:
        await owm.weather.__wrapped__((52.52, 13.405))

    assert "synthetic-canary" not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize("current,forecast", [({}, {}), (payloads()[0], {"city": {"timezone": 100000}, "list": []})])
async def test_malformed_provider_payload_gets_a_safe_error(configure, current, forecast):
    configure([Response(current), Response(forecast)])
    with pytest.raises(owm.WeatherServiceError):
        await owm.weather.__wrapped__((52.52, 13.405))


@pytest.mark.parametrize("coordinates", [(float("nan"), 0), (91, 0), (0, 181), ()])
async def test_invalid_coordinates_do_not_call_provider(configure, coordinates):
    session = configure([])
    with pytest.raises(owm.WeatherServiceError):
        await owm.weather.__wrapped__(coordinates)
    assert session.calls == []


async def test_missing_weather_key_is_reported_before_request(configure, monkeypatch):
    session = configure([])
    monkeypatch.setattr(owm.settings, "owm_key", "")
    with pytest.raises(MissingIntegration):
        await owm.weather.__wrapped__((52.52, 13.405))
    assert session.calls == []


async def test_geocoding_uses_query_parameters_and_openweather_key(configure):
    session = configure([Response([{"lat": 52.52, "lon": 13.405, "name": "Berlin", "local_names": {"ru": "Берлин"}, "country": "DE"}])])

    assert await owm.geocoding("Berlin/DE ?&") == ((52.52, 13.405), "Берлин, DE")

    assert session.calls == [
        (
            "https://api.openweathermap.org/geo/1.0/direct",
            {
                "params": {"q": "Berlin/DE ?&", "limit": 1, "appid": "synthetic-api-key"},
            },
        )
    ]


async def test_geocoding_falls_back_when_localized_name_is_absent(configure):
    configure([Response([{"lat": 1, "lon": 2, "name": "Place", "state": "Region", "country": "XX"}])])
    assert await owm.geocoding("Place") == ((1.0, 2.0), "Place, Region, XX")


async def test_geocoding_no_matches_is_distinct_from_service_failure(configure):
    configure([Response([])])
    assert await owm.geocoding("Synthetic nonexistent place") is None
    configure([Response({}, status=401)])
    with pytest.raises(owm.WeatherServiceError):
        await owm.geocoding("Place")


@pytest.fixture
def weather_commands(monkeypatch):
    monkeypatch.setitem(sys.modules, "app", SimpleNamespace(bot=SimpleNamespace(get_chat=AsyncMock())))
    spec = importlib.util.spec_from_file_location(
        "weather_output_test", Path(__file__).resolve().parents[1] / "src/msu_hub_bot/commands/weather.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def reading(at, temperature=10):
    return owm.WeatherReading(
        dt=datetime.datetime.fromisoformat(at),
        temp=temperature,
        feels_like=temperature - 1,
        weather=[owm.WeatherType(id=800, main="Clear", description="ясно <небо>", icon="01d")],
    )


def test_rendering_uses_local_dates_real_cadence_and_escaped_names(weather_commands):
    forecast = owm.WeatherForecast(
        timezone_offset=7200,
        current=reading("2026-09-16T23:00:00+00:00"),
        periods=[
            reading("2026-09-17T00:00:00+00:00", 12),
            reading("2026-09-17T03:00:00+00:00", 15),
            reading("2026-09-18T00:00:00+00:00", 8),
            reading("2026-09-18T03:00:00+00:00", 9),
        ],
    )

    result = weather_commands.parse_response(forecast, "Place <&>")

    assert "17 сентября" in result
    assert "Place &lt;&amp;&gt;" in result
    assert "ясно &lt;небо&gt;" in result
    assert "Прогноз с шагом 3 часа" in result
    assert "02:00" in result and "05:00" in result and "18.09 02:00" in result
    assert "До конца дня" in result and "Завтра" in result
    assert "Диапазоны — по точкам трёхчасового прогноза." in result
    assert "почасов" not in result.lower()


@pytest.mark.parametrize("count", [0, 1, 2])
def test_partial_forecast_never_indexes_missing_periods(weather_commands, count):
    current = reading("2026-09-16T12:00:00+00:00")
    current.weather = []
    forecast = owm.WeatherForecast(timezone_offset=0, current=current, periods=[reading("2026-09-16T15:00:00+00:00")] * count)
    result = weather_commands.parse_response(forecast, "Place")
    assert "Сейчас" in result
    assert "без описания" in result
    assert ("Прогноз пока недоступен." in result) == (count == 0)


async def test_unknown_place_gets_feedback_without_weather_request(weather_commands):
    module = weather_commands
    module.get_chat = AsyncMock(return_value=SimpleNamespace(location=None))
    module.geocoding = AsyncMock(return_value=None)
    module.weather = AsyncMock()
    message = SimpleNamespace(chat=SimpleNamespace(id=1), reply=AsyncMock(), bot=object())

    await module.Weather.process(message, SimpleNamespace(extract_text=lambda: (message, "No such place")))

    assert "Не удалось найти место" in message.reply.await_args.args[0]
    module.weather.assert_not_awaited()


async def test_weather_map_still_combines_mapbox_and_openweather_tiles(configure, monkeypatch):
    def png(color):
        file = io.BytesIO()
        Image.new("RGBA", (2, 2), color).save(file, format="PNG")
        return file.getvalue()

    session = configure([Response(png((10, 20, 30, 255))), Response(png((255, 0, 0, 128)))])
    monkeypatch.setattr(owm.settings, "mapbox_key", "synthetic-mapbox-key")
    monkeypatch.setattr(owm.settings, "owm_map_key", "synthetic-map-key")

    file = await owm.weather_map.__wrapped__(1, 0, 1)

    assert "api.mapbox.com/styles/" in session.calls[0][0]
    assert "tile.openweathermap.org/map/precipitation_new/" in session.calls[1][0]
    image = Image.open(file)
    assert image.size == (2, 2)
    assert image.getpixel((0, 0)) == (133, 10, 15, 255)
