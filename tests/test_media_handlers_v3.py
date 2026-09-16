"""Validate media handler calls through real v3 method models, without providers."""

import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.filters import CommandObject
from aiogram.methods import EditMessageText, SendAudio, SendDocument, SendMessage, SendPhoto, SendSticker
from aiogram.types import BufferedInputFile, CallbackQuery

from common.tg.filters import MetaInfo
from hub_bot.commands import animate, arxiv, camera, latex, sed, song, tesseract, tts
from telegram_helpers import make_bot, make_message


def executor(result):
    return SimpleNamespace(run=AsyncMock(return_value=(result, False)))


@pytest.mark.asyncio
async def test_tts_upload_keeps_filename_and_metadata():
    bot = make_bot()
    message = make_message(bot, text="/tts en Hello")
    audio = io.BytesIO(b"synthetic mp3")
    audio.name = "tts_hello.mp3"
    await tts.process_tts(message, MetaInfo(message, arguments=["en"], text="Hello"), executor(audio))
    sent = bot.session.methods[-1]
    assert isinstance(sent, SendAudio)
    assert isinstance(sent.audio, BufferedInputFile)
    assert sent.audio.data == b"synthetic mp3"
    assert sent.audio.filename == "tts_hello.mp3"
    assert (sent.performer, sent.title) == ("TTS En", "Hello")
    assert sent.reply_parameters.message_id == message.message_id


@pytest.mark.asyncio
async def test_long_ocr_output_uploads_a_named_utf8_document():
    bot = make_bot()
    message = make_message(bot)
    text = "Длинный текст " * 500
    meta = SimpleNamespace(extract_image_with_downloading=AsyncMock(return_value=(message, io.BytesIO(b"image"))))
    await tesseract.process_image_to_text(message, meta, executor(text))
    sent = bot.session.methods[-1]
    assert isinstance(sent, SendDocument)
    assert isinstance(sent.document, BufferedInputFile)
    assert sent.document.data.decode() == text
    assert sent.document.filename.startswith("ocr_")
    assert sent.document.filename.endswith(".txt")


@pytest.mark.asyncio
async def test_sed_preserves_reply_target_and_topic_action():
    bot = make_bot()
    reply = make_message(bot, message_id=8, text="original")
    message = make_message(bot, text="s/original/revised/", reply_to_message=reply, is_topic_message=True, message_thread_id=9)
    await sed.process_sed(message, bot, executor("<revised>"))
    action, sent = bot.session.methods
    assert action.message_thread_id == 9
    assert isinstance(sent, SendMessage)
    assert sent.text == "&lt;revised&gt;"
    assert sent.reply_parameters.message_id == 8


@pytest.mark.asyncio
async def test_arxiv_uses_explicit_command_arguments():
    bot = make_bot()
    message = make_message(bot, text="/arxiv synthetic query")
    worker = executor([])
    await arxiv.process_arxiv(message, CommandObject(command="arxiv", args="synthetic query"), worker)
    assert worker.run.call_args.args == (arxiv.arxiv_search, "synthetic query")
    assert isinstance(bot.session.methods[-1], SendMessage)


@pytest.mark.asyncio
async def test_animate_delivers_native_v3_upload():
    bot = make_bot()
    message = make_message(bot, text="/animate Привет")
    sticker = animate.animate(animate.AnimateTextSticker, "Привет")
    assert sticker is not None
    await animate.process_animate(message, MetaInfo(message, text="Привет"), executor(sticker))
    sent = bot.session.methods[-1]
    assert isinstance(sent, SendSticker)
    assert isinstance(sent.sticker, BufferedInputFile)
    assert sent.sticker.data.startswith(b"\x1f\x8b")
    assert sent.sticker.filename == "sticker.tgs"


@pytest.mark.asyncio
async def test_latex_edit_keeps_media_and_message_id():
    bot = make_bot()
    message = make_message(bot, message_id=12, text="/tex x")
    latex.Latex.replies.clear()
    try:
        await latex.Latex.process(message, MetaInfo(message, text="x"))
        assert isinstance(bot.session.methods[-1], SendPhoto)
        await latex.Latex.process_edited(message, MetaInfo(message, text="y"), bot)
        sent = bot.session.methods[-1]
        assert sent.__api_method__ == "editMessageMedia"
        assert sent.message_id == 1
        assert sent.chat_id == message.chat.id
        assert sent.media.media == latex.Codecogs.url("y")
    finally:
        latex.Latex.replies.clear()


@pytest.mark.asyncio
async def test_song_download_uses_bound_bot_and_no_size_metadata_is_allowed():
    bot = make_bot()
    message = make_message(bot, voice={"file_id": "voice", "file_unique_id": "unique", "duration": 1})
    result = json.dumps({"status": {"code": 0}, "metadata": {"music": [{"artists": [{"name": "Artist"}], "title": "Song"}]}})
    await song.process_song(message, bot, executor(result))
    assert any(method.__api_method__ == "getFile" for method in bot.session.methods)
    sent = bot.session.methods[-1]
    assert isinstance(sent, EditMessageText)
    assert "Artist — Song" in sent.text
    assert sent.link_preview_options.is_disabled


@pytest.mark.asyncio
async def test_camera_preserves_callback_wire_and_native_edit(monkeypatch):
    bot = make_bot()
    message = make_message(bot)
    query = CallbackQuery.model_validate(
        {"id": "click", "from_user": message.from_user, "chat_instance": "chat", "message": message, "data": "camera:msu:update"},
        context={"bot": bot},
    )
    monkeypatch.setattr(camera, "camera_msu", AsyncMock(return_value=io.BytesIO(b"jpeg")))
    data = camera.CameraCallback.unpack("camera:msu:update")
    await camera.Camera.process_cb(query, data, executor(None))
    acknowledgement, edit = bot.session.methods
    assert acknowledgement.__api_method__ == "answerCallbackQuery"
    assert edit.__api_method__ == "editMessageMedia"
    assert isinstance(edit.media.media, BufferedInputFile)
    assert edit.media.media.data == b"jpeg"
    assert [button.callback_data for button in edit.reply_markup.inline_keyboard[0]] == ["camera:msu:update", "camera:msu:stop"]


@pytest.mark.asyncio
async def test_camera_inline_callback_only_acknowledges():
    bot = make_bot()
    query = CallbackQuery.model_validate(
        {
            "id": "click",
            "from_user": {"id": 42, "is_bot": False, "first_name": "Test"},
            "chat_instance": "chat",
            "inline_message_id": "inline",
            "data": "camera:msu:stop",
        },
        context={"bot": bot},
    )
    await camera.Camera.process_cb(query, camera.CameraCallback(name="msu", action="stop"), executor(None))
    assert [method.__api_method__ for method in bot.session.methods] == ["answerCallbackQuery"]
