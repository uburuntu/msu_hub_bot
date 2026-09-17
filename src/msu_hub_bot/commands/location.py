import re

from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.markdown import hcode

from msu_hub_bot.telegram.filters import MetaInfo
from itertools import pairwise

pattern_fp = re.compile(r"[-+]?[0-9]*\.?[0-9]+")


def valid_lat_lon(lat: float, lon: float) -> bool:
    return -90 <= lat <= 90 and -180 <= lon <= 180


def emoji_by_longitude(lon: float) -> str:
    if 72 <= lon <= 190:
        # Asia
        return "🌏"

    if -23 <= lon <= 72:
        # Europe + Africa
        return "🌍"

    # America
    return "🌎"


async def process_location(message: Message, meta: MetaInfo) -> Message | bool | None:
    def maps_keyboard(latitude: float, longitude: float) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="Google Maps", url=f"https://maps.google.com/maps?q={latitude},{longitude}&z=16"),
                    InlineKeyboardButton(text="Yandex Maps", url=f"https://yandex.ru/maps/?pt={longitude},{latitude}&z=16"),
                ],
                [
                    InlineKeyboardButton(text="2GIS", url=f"https://2gis.ru/geo/{longitude},{latitude}"),
                ],
            ]
        )

    target, text = meta.extract_text()

    if location := (target.venue and target.venue.location or target.location):
        lat, lon = location.latitude, location.longitude
        return await message.reply(
            f"{emoji_by_longitude(lon)} Latitude Longitude:\n" + hcode(f"{lat}, {lon}"), reply_markup=maps_keyboard(lat, lon)
        )

    if text:
        for raw_lat, raw_lon in pairwise(pattern_fp.findall(text)):
            lat, lon = float(raw_lat), float(raw_lon)
            if valid_lat_lon(lat, lon):
                break
        else:
            return await message.reply(hcode("🌎 The latitude must be a number between -90 and 90 and the longitude between -180 and 180"))

        return await target.reply_location(lat, lon, reply_markup=maps_keyboard(lat, lon))

    return True
