from types import SimpleNamespace

import pytest
from aiogram import Bot, types

from common.tg.filters import MetaCommand, SimpleExtractor


@pytest.fixture
def bot(monkeypatch):
    bot = Bot("123456789:" + "a" * 35)
    bot._me = types.User(id=123456789, is_bot=True, first_name="Test", username="test_bot")
    monkeypatch.setattr(types.Message, "bot", property(lambda self: bot))
    return bot


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["", " ", "\n\t", "/unrelated text", "/tr@another_bot text"])
async def test_nonmatching_input_is_ignored(bot, text):
    assert await MetaCommand("tr").check(types.Message(text=text)) is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text,expected",
    [
        ("/tr please keep /tr here", "please keep /tr here"),
        ("  /Tr@TEST_BOT Привет /Tr@TEST_BOT", "Привет /Tr@TEST_BOT"),
        ("prefix #tr text #tr", "prefix  text #tr"),
    ],
)
async def test_only_matched_command_token_is_removed(bot, text, expected):
    result = await MetaCommand("tr").check(types.Message(text=text))
    assert result["meta"].text == expected


@pytest.mark.asyncio
async def test_caption_arguments_and_reply_selection(bot):
    reply = types.Message(text="Use the reply")
    message = types.Message(caption="/tr en ru keep /tr", reply_to_message=reply)
    result = await MetaCommand("tr", args=2).check(message)
    assert result["meta"].arguments == ["en", "ru"]
    assert result["meta"].extract_text() == (message, "keep /tr")
    message = types.Message(text="/tr en ru", reply_to_message=reply)
    result = await MetaCommand("tr", args=2).check(message)
    assert result["meta"].extract_text() == (reply, "Use the reply")


@pytest.mark.asyncio
@pytest.mark.parametrize("animated,video,is_image", [(False, False, True), (True, False, False), (False, True, False)])
async def test_sticker_media_types(animated, video, is_image):
    sticker = SimpleNamespace(is_animated=animated, is_video=video)
    message = SimpleNamespace(photo=None, document=None, sticker=sticker)
    assert (await SimpleExtractor.image(message) is sticker) == is_image
