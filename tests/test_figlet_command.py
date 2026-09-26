"""The canonical Figlet command retains playful rendering without clipping output."""

from itertools import cycle
from unittest.mock import AsyncMock

import pytest
from aiogram.types import Update
from teleforge import App
from teleforge.testing import RecordingBot
from transliterate import translit

from msu_hub_bot.commands import figlet
from msu_hub_bot.execution.executor import TPExecutor
from msu_hub_bot.features.command import format_input_error
from telegram_helpers import make_message


@pytest.fixture
async def setup(monkeypatch):
    monkeypatch.setattr(figlet, "figlets", cycle(figlet.figlet_fonts))
    executor = TPExecutor(1)
    bot = RecordingBot()
    app = App(data={"cpu_executor": executor}, input_formatter=format_input_error).include(figlet.FigletFeature())
    try:
        yield app, bot, executor
    finally:
        await app.aclose()
        executor.shutdown(wait=True)
        await bot.session.close()


async def test_default_and_font_rotation_keep_native_rendering(setup):
    app, bot, _ = setup
    for index, font in enumerate((*figlet.figlet_fonts, figlet.figlet_fonts[0]), 1):
        message = make_message(bot, message_id=index, text="/figlet", is_topic_message=True, message_thread_id=55)

        await app.feed_update(bot, Update(update_id=index, message=message))

        sent = bot.requests[-1]
        assert sent.__api_method__ == "sendMessage"
        assert sent.text == font.renderText("kek")
        assert sent.entities[0].type == "pre" and sent.parse_mode is None
        assert sent.reply_parameters.message_id == index and sent.message_thread_id == 55


@pytest.mark.parametrize("text", ["/FIGLET Привет", "Привет #figlet"])
async def test_explicit_text_transliterates_and_targets_invocation(setup, text):
    app, bot, _ = setup
    reply = make_message(bot, message_id=7, text="другой текст")
    message = make_message(bot, text=text, reply_to_message=reply)

    await app.feed_update(bot, Update(update_id=1, message=message))

    sent = bot.requests[-1]
    assert sent.text == figlet.figlet_fonts[0].renderText("Privet")
    assert sent.reply_parameters.message_id == message.message_id


@pytest.mark.parametrize("command,target_id", [("/figlet", 7), ("/figlet kek", 1)])
async def test_default_on_captionless_reply_preserves_target_without_retargeting_explicit_text(setup, command, target_id):
    app, bot, _ = setup
    reply = make_message(
        bot,
        message_id=7,
        photo=[{"file_id": "photo", "file_unique_id": "p", "width": 20, "height": 20}],
        is_topic_message=True,
        message_thread_id=55,
    )
    message = make_message(bot, text=command, reply_to_message=reply, is_topic_message=True, message_thread_id=55)

    await app.feed_update(bot, Update(update_id=1, message=message))

    sent = bot.requests[-1]
    assert sent.text == figlet.figlet_fonts[0].renderText("kek")
    assert sent.reply_parameters.message_id == target_id and sent.message_thread_id == 55


async def test_long_render_is_complete_file_on_selected_reply_topic(setup):
    app, bot, _ = setup
    text = "Всем привет! " * 80
    expected = figlet.figlet_fonts[0].renderText(translit(text, "ru", reversed=True))
    assert len(expected) > 4096
    reply = make_message(bot, message_id=7, text=text, is_topic_message=True, message_thread_id=55)
    message = make_message(bot, text="/figlet", reply_to_message=reply, is_topic_message=True, message_thread_id=55)

    await app.feed_update(bot, Update(update_id=1, message=message))

    assert len(bot.requests) == 1
    sent = bot.requests[0]
    assert sent.__api_method__ == "sendDocument"
    assert bot.recording.uploads[0]["document"] == expected.encode("utf-8")
    assert sent.reply_parameters.message_id == 7 and sent.message_thread_id == 55


async def test_oversized_rich_reply_is_rejected_before_rendering(setup, monkeypatch):
    app, bot, executor = setup
    render = AsyncMock(side_effect=AssertionError("Oversized input must not enter the worker"))
    monkeypatch.setattr(executor, "run", render)
    reply = make_message(bot, message_id=7, rich_message={"blocks": [{"type": "paragraph", "text": "А" * 4097}]})
    message = make_message(bot, text="/figlet", reply_to_message=reply)

    await app.feed_update(bot, Update(update_id=1, message=message))

    render.assert_not_awaited()
    assert bot.requests[-1].text == "Текст слишком длинный. Попробуй уложиться в 4096 символов."
    assert bot.requests[-1].reply_parameters.message_id == message.message_id


@pytest.mark.parametrize("source_text", ["Привет", None])
async def test_worker_deadline_has_useful_guidance_at_invocation(setup, monkeypatch, source_text):
    app, bot, executor = setup
    monkeypatch.setattr(executor, "run", AsyncMock(return_value=(None, True)))
    reply = make_message(bot, message_id=7, text=source_text, is_topic_message=True, message_thread_id=55)
    message = make_message(bot, text="/figlet", reply_to_message=reply, is_topic_message=True, message_thread_id=55)

    await app.feed_update(bot, Update(update_id=1, message=message))

    assert len(bot.requests) == 1
    assert bot.requests[0].text == "🤷🏻‍♂️ Не успел нарисовать буквы. Попробуй текст покороче."
    assert bot.requests[0].reply_parameters.message_id == message.message_id
    assert bot.requests[0].message_thread_id == 55


@pytest.mark.parametrize("command", ["/figlet", "/figlet 🙂"])
async def test_unsupported_symbols_have_guidance_at_invocation(setup, command):
    app, bot, _ = setup
    reply = make_message(bot, message_id=7, text="🙂", is_topic_message=True, message_thread_id=55)
    message = make_message(bot, text=command, reply_to_message=reply, is_topic_message=True, message_thread_id=55)

    await app.feed_update(bot, Update(update_id=1, message=message))

    assert len(bot.requests) == 1
    assert bot.requests[0].text == "Этот шрифт не умеет рисовать такие символы. Попробуй буквы или цифры."
    assert bot.requests[0].reply_parameters.message_id == message.message_id
    assert bot.requests[0].message_thread_id == 55


async def test_caption_command_remains_outside_figlet_text_route(setup, monkeypatch):
    app, bot, executor = setup
    render = AsyncMock(side_effect=AssertionError("Caption was not a native Figlet trigger"))
    monkeypatch.setattr(executor, "run", render)
    message = make_message(bot, caption="/figlet Привет", photo=[{"file_id": "p", "file_unique_id": "p", "width": 20, "height": 20}])

    await app.feed_update(bot, Update(update_id=1, message=message))

    render.assert_not_awaited()
    assert not bot.requests
