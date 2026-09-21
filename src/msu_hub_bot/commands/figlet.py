from itertools import cycle

from aiogram.utils.formatting import Pre
from pyfiglet import Figlet
from transliterate import translit

from msu_hub_bot.telegram.command_api import MetaCommand, TextInput

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


@MetaCommand("figlet", text=TextInput(reply=True, max_chars=200), rich=False, soft_messages=1)
async def process_figlet(text: str = "kek") -> Pre:
    text = translit(text, "ru", reversed=True)
    figlet = next(figlets)
    return Pre(figlet.renderText(text))
