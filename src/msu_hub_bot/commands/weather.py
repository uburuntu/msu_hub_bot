import datetime
import math
import time
from contextlib import suppress
from copy import copy
from typing import Tuple

from aiogram import Bot, html
from aiogram.exceptions import TelegramBadRequest
import cachetools
from msu_hub_bot.caching import cached_async
from aiogram.types import Message, ChatFullInfo, CallbackQuery, InputFile, InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto
from aiogram.filters.callback_data import CallbackData
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.utils.markdown import hbold, hitalic, hcode

from msu_hub_bot.telegram.context import bot_for
from msu_hub_bot.telegram.files import input_file
from msu_hub_bot.providers.owm import WeatherForecast, WeatherReading, weather, id_to_emoji, weather_map, coordinates_to_xy, geocoding
from msu_hub_bot.telegram.callbacks import CallbackCommandBase
from msu_hub_bot.telegram.filters import MetaInfo


@cached_async(ttl=10 * 60)
async def get_chat(chat_id: int, bot: Bot) -> ChatFullInfo:
    return await bot.get_chat(chat_id)


def parse_response(forecast: WeatherForecast, location_name: str) -> str:
    curr = forecast.current
    timezone = datetime.timezone(datetime.timedelta(seconds=forecast.timezone_offset))
    today = curr.dt.astimezone(timezone).date()
    periods = [period for period in forecast.periods if period.dt >= curr.dt]

    def pretty_date(d: datetime.date) -> str:
        month_names = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря")
        weekday_names = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")
        return f"{d.day} {month_names[d.month - 1]}, {weekday_names[d.weekday()]}"

    def condition(reading: WeatherReading) -> str:
        if not reading.weather:
            return hitalic("без описания")
        weather_type = reading.weather[0]
        return hitalic(f"{id_to_emoji(weather_type.id)} {weather_type.description}")

    def temp(t: float) -> str:
        return hitalic(f"{int(t)}°C")

    def line_period(period: WeatherReading) -> str:
        local = period.dt.astimezone(timezone)
        label = local.strftime("%H:%M" if local.date() == today else "%d.%m %H:%M")
        return f"{hbold(label)}: {temp(period.temp)} | ощущается как {temp(period.feels_like)}, {condition(period)}"

    def line_range(description: str, readings: list[WeatherReading]) -> str:
        start, end = temp(min(reading.temp for reading in readings)), temp(max(reading.temp for reading in readings))
        temp_text = f"от {start} до {end}" if start != end else f"{start}"
        return f"{hbold(description)}: {temp_text}"

    lines = [
        f"{hbold('Погода')}: {html.quote(location_name)}, {pretty_date(today)}",
        "",
        f"{hbold('Сейчас')}: {temp(curr.temp)} | ощущается как {temp(curr.feels_like)}, {condition(curr)}",
    ]
    if periods:
        lines.extend(["", hbold("Прогноз с шагом 3 часа"), *(line_period(period) for period in periods[:3])])
        ranges = []
        for day, label in ((today, "До конца дня"), (today + datetime.timedelta(days=1), "Завтра")):
            readings = [period for period in periods if period.dt.astimezone(timezone).date() == day]
            if readings:
                ranges.append(line_range(label, readings))
        if ranges:
            lines.extend(["", *ranges, hitalic("Диапазоны — по точкам трёхчасового прогноза.")])
    else:
        lines.extend(["", "Прогноз пока недоступен."])
    return "\n".join(lines)


class WeatherCallback(CallbackData, prefix="weather"):
    lat: float
    lon: float


class Weather(CallbackCommandBase):
    Moscow = (55.7522, 37.6155)

    replies: cachetools.LRUCache[tuple[int, int], int] = cachetools.LRUCache(maxsize=128)
    location_refreshes: cachetools.LRUCache[tuple[int, int], float] = cachetools.LRUCache(maxsize=128)
    location_refresh_interval = 15 * 60
    callback_data = WeatherCallback

    @classmethod
    def keyboard(cls, coordinates: Tuple[float, float]) -> InlineKeyboardMarkup:
        keyboard = InlineKeyboardBuilder().row(
            InlineKeyboardButton(text="🔄 Обновить", callback_data=WeatherCallback(lat=coordinates[0], lon=coordinates[1]).pack()),
        )
        return InlineKeyboardMarkup(inline_keyboard=keyboard.export())

    @classmethod
    async def process(cls, message: Message, meta: MetaInfo) -> Message | bool | None:
        chat = await get_chat(message.chat.id, bot_for(message))
        target, text = meta.extract_text()

        if text:
            result = await geocoding(text)
            if not result:
                return await target.reply("Не удалось найти место. Уточните название или пришлите геопозицию.")
            coordinates, location_name = result

        elif loc := chat.location or target.venue:
            coordinates = loc.location.latitude, loc.location.longitude
            location_name = loc.address

        elif target.location:
            coordinates = target.location.latitude, target.location.longitude
            location_name = None

        else:
            coordinates, location_name = cls.Moscow, "Москва"

        response = await weather(coordinates, location_name)
        if response is None:
            return True

        text = parse_response(*response)
        return await target.reply(text, reply_markup=cls.keyboard(coordinates), disable_web_page_preview=True)

    @classmethod
    async def process_location(cls, message: Message) -> Message | bool | None:
        location, location_name = message.location, None
        if venue := message.venue:
            location, location_name = venue.location, venue.address

        if location is None:
            return True
        coordinates = (location.latitude, location.longitude)
        response = await weather(coordinates, location_name)
        if response is None:
            return True

        text = parse_response(*response)
        result = await message.reply(text, reply_markup=cls.keyboard(coordinates), disable_web_page_preview=True)
        key = cls.cache_key(message)
        cls.replies[key] = result.message_id
        cls.location_refreshes[key] = time.monotonic()
        return result

    @classmethod
    async def process_location_edited(cls, message: Message) -> Message | bool | None:
        key = cls.cache_key(message)
        if key not in cls.replies:
            return True

        async with cls.lock(key):
            message_id = cls.replies.get(key)
            if message_id is None:
                return True
            now = time.monotonic()
            if now - cls.location_refreshes.get(key, float("-inf")) < cls.location_refresh_interval:
                return True

            location, location_name = message.location, None
            if venue := message.venue:
                location, location_name = venue.location, venue.address
            if location is None:
                return True

            # Intermediate edits are coalesced into the next eligible received position.
            # No timer or location data survives the bounded reply cache/restart.
            cls.location_refreshes[key] = now
            coordinates = (location.latitude, location.longitude)
            try:
                response = await weather(coordinates, location_name)
                if response is None:
                    return True

                with suppress(TelegramBadRequest):
                    text = parse_response(*response)
                    return await bot_for(message).edit_message_text(
                        text,
                        chat_id=message.chat.id,
                        message_id=message_id,
                        reply_markup=cls.keyboard(coordinates),
                        disable_web_page_preview=True,
                    )
            finally:
                # Include provider/edit latency in the interval between completed refreshes.
                if key in cls.replies:
                    cls.location_refreshes[key] = time.monotonic()

        return None

    @classmethod
    async def process_cb(cls, query: CallbackQuery, callback_data: WeatherCallback) -> Message | bool | None:
        try:
            coordinates = float(callback_data.lat), float(callback_data.lon)
            if not all(math.isfinite(value) for value in coordinates) or not (
                -90 <= coordinates[0] <= 90 and -180 <= coordinates[1] <= 180
            ):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            return await query.answer("Не удалось прочитать координаты. Пришлите геопозицию заново.", show_alert=True)
        if not isinstance(query.message, Message):
            return await query.answer("Сообщение с погодой больше недоступно.", show_alert=True)

        await query.answer(text="✅", cache_time=2 * 60)

        response = await weather(coordinates, None)
        if response is None:
            return True

        text = parse_response(*response)
        with suppress(TelegramBadRequest):
            return await query.message.edit_text(text, reply_markup=cls.keyboard(coordinates), disable_web_page_preview=True)

        return None


class WeatherMapCallback(CallbackData, prefix="map"):
    action: str
    lat: float
    lon: float
    zoom: int


class WeatherMap(CallbackCommandBase):
    MSU = (55.7031, 37.5311)

    file_ids: cachetools.TTLCache[tuple[int, int, int], str] = cachetools.TTLCache(maxsize=512, ttl=30 * 60)
    callback_data = WeatherMapCallback

    @classmethod
    def keyboard(cls, coordinates: Tuple[float, float], zoom: int) -> InlineKeyboardMarkup:
        keyboard = InlineKeyboardBuilder().row(
            InlineKeyboardButton(
                text="➕", callback_data=WeatherMapCallback(action="zoom_in", lat=coordinates[0], lon=coordinates[1], zoom=zoom).pack()
            ),
            InlineKeyboardButton(
                text="🔄", callback_data=WeatherMapCallback(action="update", lat=coordinates[0], lon=coordinates[1], zoom=zoom).pack()
            ),
            InlineKeyboardButton(
                text="➖", callback_data=WeatherMapCallback(action="zoom_out", lat=coordinates[0], lon=coordinates[1], zoom=zoom).pack()
            ),
        )
        return InlineKeyboardMarkup(inline_keyboard=keyboard.export())

    @classmethod
    async def process(cls, message: Message) -> Message | bool | None:
        coordinates = cls.MSU

        if reply_to := message.reply_to_message:
            if reply_to.location:
                loc = reply_to.location
                coordinates = loc.latitude, loc.longitude
            elif reply_to.venue:
                loc = reply_to.venue.location
                coordinates = loc.latitude, loc.longitude
            else:
                chat = await get_chat(message.chat.id, bot_for(message))

                if chat.location:
                    coordinates = chat.location.location.latitude, chat.location.location.longitude

        zoom = 13
        x, y = coordinates_to_xy(coordinates, zoom)
        key = (x, y, zoom)

        file: InputFile | str
        if key in cls.file_ids:
            file = cls.file_ids[key]
        else:
            file = input_file(copy(await weather_map(x, y, zoom)), "weather-map.png")

        text = (
            f"{hbold('Latitude')}: {hcode(coordinates[0])}\n"
            f"{hbold('Longitude')}: {hcode(coordinates[1])}\n"
            f"{hbold('Zoom Level')}: {hcode(zoom)}"
        )

        result = await message.reply_photo(file, caption=text, reply_markup=cls.keyboard(coordinates, zoom))

        if key not in cls.file_ids and result.photo:
            cls.file_ids[(x, y, zoom)] = result.photo[-1].file_id

        return result

    @classmethod
    async def process_cb(cls, query: CallbackQuery, callback_data: WeatherMapCallback) -> Message | bool | None:
        message = query.message
        if not isinstance(message, Message):
            return await query.answer("Сообщение с картой больше недоступно.")
        action, zoom = callback_data.action, int(callback_data.zoom)
        coordinates = float(callback_data.lat), float(callback_data.lon)

        if action == "zoom_in":
            zoom += 1
        elif action == "zoom_out":
            zoom -= 1

        zoom_l, zoom_r = 1, 18
        if zoom < zoom_l:
            zoom = zoom_r
        elif zoom > zoom_r:
            zoom = zoom_l

        x, y = coordinates_to_xy(coordinates, zoom)
        key = (x, y, zoom)

        file: InputFile | str
        if key in cls.file_ids:
            file = cls.file_ids[key]
        else:
            file = input_file(copy(await weather_map(x, y, zoom)), "weather-map.png")

        await query.answer(text="✅", cache_time=1)

        text = (
            f"{hbold('Latitude')}: {hcode(coordinates[0])}\n"
            f"{hbold('Longitude')}: {hcode(coordinates[1])}\n"
            f"{hbold('Zoom Level')}: {hcode(zoom)}"
        )

        result = None
        with suppress(TelegramBadRequest):
            result = await message.edit_media(InputMediaPhoto(media=file, caption=text), reply_markup=cls.keyboard(coordinates, zoom))

        if isinstance(result, Message) and result.photo and key not in cls.file_ids:
            cls.file_ids[(x, y, zoom)] = result.photo[-1].file_id

        return result
