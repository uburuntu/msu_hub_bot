from msu_hub_bot.settings import settings

import asyncio
import datetime
import io
import math
from typing import List, Tuple, Optional

import aiohttp
from PIL import Image, ImageOps
from aiocache import cached
from pydantic import BaseModel, Field

from common.externals.exceptions import ExternalServiceError
from common.utils import bytes_io, image_bytes_io


class WeatherType(BaseModel):
    id: int
    main: str
    description: str
    icon: str


class WeatherReading(BaseModel):
    dt: datetime.datetime
    temp: float
    feels_like: float
    weather: List[WeatherType] = Field(default_factory=list)


class WeatherForecast(BaseModel):
    timezone_offset: int = Field(ge=-86399, le=86399)
    current: WeatherReading
    periods: List[WeatherReading]


class WeatherServiceError(ExternalServiceError):
    def __init__(self, status: int = None):
        self.status = status
        super().__init__("Не удалось получить погоду. Попробуйте ещё раз позже.")


def validate_coordinates(coordinates: Tuple[float, float]) -> Tuple[float, float]:
    lat, lon = float(coordinates[0]), float(coordinates[1])
    if not math.isfinite(lat) or not math.isfinite(lon) or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("Invalid coordinates")
    return lat, lon


async def _get_json(session: aiohttp.ClientSession, url: str, params: dict):
    async with session.get(url, params=params) as response:
        if response.status != 200:
            raise WeatherServiceError(response.status)
        return await response.json()


def id_to_emoji(weather_id: int) -> str:
    # Condition codes: https://openweathermap.org/weather-conditions

    if weather_id in range(200, 203):
        return "⛈"

    if weather_id in range(230, 233):
        return "⛈"

    if weather_id in range(200, 300):
        return "🌩"

    if weather_id in range(300, 400):
        return "🌦"

    if weather_id in range(600, 700) or weather_id == 511:
        return "❄️"

    if weather_id in range(500, 600):
        return "☔️"

    if weather_id == 781:
        return "🌪"

    if weather_id in range(700, 800):
        return "🌫"

    if weather_id == 800:
        return "☀️"

    if weather_id == 801:
        return "🌤"

    if weather_id == 802:
        return "⛅️"

    if weather_id == 803:
        return "🌥"

    if weather_id in range(804, 900):
        return "☁️"

    return "☁️"


def coordinates_to_xy(coordinates: Tuple[float, float], zoom: int):
    # https://wiki.openstreetmap.org/wiki/Slippy_map_tilenames
    lat_rad = math.radians(coordinates[0])
    n = 2.0**zoom
    x_tile = int((coordinates[1] + 180.0) / 360.0 * n)
    y_tile = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
    return x_tile, y_tile


@cached(ttl=2 * 60)
async def weather(coordinates: Tuple[float, float], location_name: str = None) -> Tuple[WeatherForecast, str]:
    """Combine current weather and the forecast available at three-hour intervals."""
    key = settings.require("owm_key")
    try:
        lat, lon = validate_coordinates(coordinates)
        params = dict(lat=lat, lon=lon, units="metric", lang="ru", appid=key)
        async with asyncio.timeout(25), aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            current = await _get_json(session, "https://api.openweathermap.org/data/2.5/weather", params)
            forecast = await _get_json(session, "https://api.openweathermap.org/data/2.5/forecast", params)

        def reading(item):
            return WeatherReading(dt=item["dt"], weather=item.get("weather", []), **item["main"])

        result = WeatherForecast(
            timezone_offset=forecast["city"]["timezone"],
            current=reading(current),
            periods=sorted((reading(item) for item in forecast["list"]), key=lambda period: period.dt),
        )
        name = location_name or current.get("name") or forecast["city"].get("name") or f"{lat:g}, {lon:g}"
        return result, name
    except (aiohttp.ClientError, TimeoutError, KeyError, TypeError, ValueError, IndexError):
        # Transport errors can contain a URL with appid; keep that out of logs.
        raise WeatherServiceError() from None


async def geocoding(name: str) -> Optional[Tuple[Tuple[float, float], str]]:
    """Resolve a place using the same OpenWeather credential as its forecast."""
    key = settings.require("owm_key")
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            result = await _get_json(session, "https://api.openweathermap.org/geo/1.0/direct", dict(q=name[:256], limit=1, appid=key))
        if not isinstance(result, list):
            raise ValueError
        if not result:
            return None
        place = result[0]
        coordinates = validate_coordinates((place["lat"], place["lon"]))
        name = (place.get("local_names") or {}).get("ru") or place["name"]
        description = ", ".join(str(value) for value in (name, place.get("state"), place.get("country")) if value)
        return coordinates, description
    except (aiohttp.ClientError, TimeoutError, KeyError, TypeError, ValueError, IndexError):
        raise WeatherServiceError() from None


@cached(ttl=2 * 60)
async def weather_map(x: int, y: int, zoom: int = 13) -> Optional[io.BytesIO]:
    # OWM Docs: https://openweathermap.org/api/weathermaps
    # Mapbox Docs: https://docs.mapbox.com/api/maps/#static-tiles

    owm_key = settings.require("owm_map_key")
    mapbox_key = settings.require("mapbox_key")

    async with aiohttp.ClientSession() as session:
        mapbox_url = f"https://api.mapbox.com/styles/v1/mapbox/satellite-streets-v11/tiles/512/{zoom}/{x}/{y}?access_token={mapbox_key}"
        async with session.get(mapbox_url) as response:
            if response.status != 200:
                return None
            content = await response.read()
            background = Image.open(bytes_io(content)).convert("RGBA")

        owm_url = f"https://tile.openweathermap.org/map/precipitation_new/{zoom}/{x}/{y}.png?appid={owm_key}"
        async with session.get(owm_url) as response:
            if response.status != 200:
                return None
            content = await response.read()
            layer = Image.open(bytes_io(content)).convert("RGBA")

    layer = ImageOps.fit(layer, (background.width, background.height), Image.LANCZOS)
    background.alpha_composite(layer)
    return image_bytes_io(background, filename="weather_map", ext="png")
