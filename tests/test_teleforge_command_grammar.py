"""Keep the existing hashtag channels distinct through signature-based dispatch."""

import pytest
from aiogram.types import Update
from teleforge import App, Feature, TextInput
from teleforge.testing import RecordingBot

from msu_hub_bot.features.command import command
from telegram_helpers import make_message


class Grammar(Feature, key="grammar"):
    @command("roll")
    async def roll(self, digits: int = 3) -> str:
        return str(digits)

    @command("meme", text=TextInput())
    async def meme(self, text: str) -> str:
        return text


@pytest.mark.parametrize(
    "text,expected",
    [
        ("10 #roll", "3"),
        ("#roll 10", "3"),
        ("#roll_10", "10"),
        ("/roll 10", "10"),
        ("#meme_extra Caption", "Caption"),
        ("10 #meme", "10"),
        ("/meme 10", "10"),
    ],
)
async def test_custom_grammar_keeps_arguments_and_text_independent(text, expected):
    bot = RecordingBot()
    async with App(Grammar()) as app:
        await app.feed_update(bot, Update(update_id=1, message=make_message(bot, text=text)))
    assert bot.requests[-1].text == expected


def test_aliases_have_one_declaration_source():
    aliases = ("test", "тест", "another")

    class Aliases(Feature, key="aliases"):
        @command(*aliases)
        async def run(self) -> str:
            return "ok"

    app = App(Aliases())
    handler = app.iter_handlers()[0]
    assert handler.declaration.names == aliases
    assert handler.declaration.metadata["_command_filter"].commands == aliases
