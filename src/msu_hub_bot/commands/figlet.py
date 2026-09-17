from itertools import cycle

from aiogram.types import Message
from aiogram.utils.markdown import hpre
from pyfiglet import Figlet
from transliterate import translit

from msu_hub_bot.telegram.filters import MetaInfo

figlet_fonts = (
    Figlet(font="3-d"),
    Figlet(font="alphabet"),
    Figlet(font="banner3"),
    Figlet(font="barbwire"),
    Figlet(font="basic"),
    Figlet(font="big"),
    Figlet(font="block"),
    Figlet(font="isometric2"),
    Figlet(font="larry3d"),
    Figlet(font="lean"),
    Figlet(font="marquee"),
    Figlet(font="rev"),
    Figlet(font="roman"),
    Figlet(font="speed"),
    Figlet(font="standard"),
)
figlets = cycle(figlet_fonts)


async def process_figlet(_message: Message, meta: MetaInfo) -> Message:
    target, text = meta.extract_text()
    if not text:
        text = "kek"

    text = translit(text, "ru", reversed=True)
    figlet = next(figlets)
    return await target.reply(hpre(figlet.renderText(text)[:4096]))
