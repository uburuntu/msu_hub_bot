"""Rotating ASCII-art fonts with complete, bounded Telegram output."""

from itertools import cycle

from aiogram import F
from aiogram.enums import ContentType
from aiogram.utils.formatting import Pre
from pyfiglet import Figlet
from teleforge import Feature
from teleforge.context import MessageContext
from teleforge.inputs import TextInput
from transliterate import translit

from msu_hub_bot.execution.executor import TPExecutor
from msu_hub_bot.features.command import command as hub_command

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


class FigletFeature(Feature, key="figlet"):
    @hub_command(
        "figlet",
        text=TextInput(max_chars=4096),
        filters=(F.content_type == ContentType.TEXT,),
        flags={"handler_key": "process_figlet", "fsm_release": True},
        rich=False,
        soft_messages=1,
    )
    async def process_figlet(self, ctx: MessageContext, text: str = "kek", *, cpu_executor: TPExecutor) -> Pre | None:
        # Even the default joke replies to the message the user selected.
        if "text" not in ctx.input_sources and ctx.message.reply_to_message:
            ctx.response_target = ctx.message.reply_to_message
        rendered, timed_out = await cpu_executor.run(next(figlets).renderText, translit(text, "ru", reversed=True), timeout=10)
        if timed_out:
            await ctx.reply("🤷🏻‍♂️ Не успел нарисовать буквы. Попробуй текст покороче.", to=ctx.message)
            return None
        if not rendered.strip():
            await ctx.reply("Этот шрифт не умеет рисовать такие символы. Попробуй буквы или цифры.", to=ctx.message)
            return None
        return Pre(rendered)
