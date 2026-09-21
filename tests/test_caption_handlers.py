"""Caption commands retain media selection, reply policy and stream ownership."""

import io
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image
from aiogram.enums import ChatAction
from aiogram.types import PhotoSize

from msu_hub_bot.commands import lobster
from msu_hub_bot.media.caption_layout import CaptionLayoutError
from msu_hub_bot.telegram.filters import MetaInfo
from msu_hub_bot.telegram.command_api import invoke_command
from msu_hub_bot.telegram.extraction import SimpleExtractor
from msu_hub_bot.telegram.responses import ResponseDeliveryError
from msu_hub_bot.telegram.media_jobs import DownloadUnavailable
from telegram_helpers import make_bot, make_message

STYLES = [("process_lobster", "lobster"), ("process_demotivator", "demotivator"), ("process_meme", "meme")]


def media_fields(kind):
    media = {"file_id": "source", "file_unique_id": "unique", "file_size": 100}
    thumbnail = {"file_id": "thumbnail", "file_unique_id": "thumb", "width": 16, "height": 16}
    if kind == "photo":
        return {"photo": [{**media, "width": 320, "height": 240}]}
    if kind == "document":
        return {"document": {**media, "mime_type": "video/mp4", "thumbnail": thumbnail}}
    if kind == "image_document":
        return {"document": {**media, "mime_type": "image/png", "thumbnail": thumbnail}}
    if kind in {"sticker", "video_sticker"}:
        return {
            "sticker": {
                **media,
                "width": 512,
                "height": 512,
                "type": "regular",
                "is_animated": False,
                "is_video": kind == "video_sticker",
                "thumbnail": thumbnail,
            }
        }
    if kind == "video_note":
        return {"video_note": {**media, "duration": 1, "length": 320, "thumbnail": thumbnail}}
    return {kind: {**media, "width": 320, "height": 240, "duration": 1, "thumbnail": thumbnail}}


@pytest.fixture
async def bot():
    bot = make_bot()
    try:
        yield bot
    finally:
        await bot.session.close()


@pytest.fixture
def actions(monkeypatch):
    recorded = []

    @asynccontextmanager
    async def action(message, kind):
        recorded.append(("start", kind))
        try:
            yield
        finally:
            recorded.append(("stop", kind))

    monkeypatch.setattr(lobster, "ChatActioner", action)
    return recorded


@pytest.fixture
def downloaded(monkeypatch):
    call = AsyncMock()
    monkeypatch.setattr(lobster, "run_downloaded", call)
    return call


@pytest.mark.parametrize("handler,style", STYLES)
@pytest.mark.parametrize("private", [False, True])
async def test_image_caption_keeps_long_text_reply_topic_and_closes_rendered_image(bot, actions, downloaded, handler, style, private):
    chat = {"id": 42, "type": "private"} if private else {"id": -1001234567890, "type": "supergroup"}
    topic = {} if private else {"is_topic_message": True, "message_thread_id": 55}
    target = make_message(bot, message_id=10, chat=chat, **topic, **media_fields("photo"))
    text = 'Привет: "[100%]" \\ путь. ' * 150
    message = make_message(bot, text=f"/meme {text}", chat=chat, reply_to_message=target, **topic)
    result = Image.new("RGB", (320, 240), "red")
    downloaded.return_value = result, False

    await invoke_command(getattr(lobster, handler), message, meta=MetaInfo(message, text=text), cpu_executor=SimpleNamespace())

    assert downloaded.call_args.args[1].file_id == "source"
    assert downloaded.call_args.args[2:] == (lobster.caption_image, text.strip(), style)
    sent = bot.session.methods[-1]
    assert sent.__api_method__ == "sendPhoto"
    assert sent.reply_parameters.message_id == 10
    assert sent.message_thread_id == (None if private else 55)
    assert (sent.reply_markup is None) == private
    assert sent.photo.filename == f"{style}.png"
    assert sent.caption is None
    with Image.open(io.BytesIO(sent.photo.data)) as image:
        assert image.size == (320, 240)
    with pytest.raises(ValueError):
        result.getpixel((0, 0))
    assert actions == [("start", ChatAction.UPLOAD_PHOTO), ("stop", ChatAction.UPLOAD_PHOTO)]


@pytest.mark.parametrize("handler,style", STYLES)
@pytest.mark.parametrize("kind", ["video", "animation", "video_note", "video_sticker", "document"])
async def test_all_video_inputs_use_full_media_not_thumbnail(bot, actions, downloaded, handler, style, kind):
    target = make_message(bot, message_id=10, **media_fields(kind))
    text = "длинная подпись " * 150
    message = make_message(bot, text=f"/meme {text}", reply_to_message=target)
    result = io.BytesIO(b"converted mp4")
    downloaded.return_value = result, False

    await invoke_command(getattr(lobster, handler), message, meta=MetaInfo(message, text=text), cpu_executor=SimpleNamespace())

    assert downloaded.call_args.args[1].file_id == "source"
    assert downloaded.call_args.args[2:] == (lobster.caption_video, text.strip(), style)
    sent = bot.session.methods[-1]
    assert sent.__api_method__ == "sendVideo"
    assert sent.reply_parameters.message_id == 10
    assert sent.video.data == b"converted mp4"
    assert sent.video.filename == f"{style}.mp4"
    assert sent.supports_streaming is True
    assert sent.reply_markup is not None
    assert sent.caption is None
    assert result.closed
    assert actions == [("start", ChatAction.UPLOAD_VIDEO), ("stop", ChatAction.UPLOAD_VIDEO)]


@pytest.mark.parametrize(
    "origin,reply", [("photo", "video"), ("image_document", "animation"), ("sticker", "video_sticker"), ("video", "photo")]
)
async def test_attached_media_has_priority_over_reply_media(bot, actions, downloaded, origin, reply):
    target = make_message(bot, message_id=10, **media_fields(reply))
    message = make_message(bot, message_id=11, caption="/meme мой кот", reply_to_message=target, **media_fields(origin))
    is_video = origin == "video"
    downloaded.return_value = (io.BytesIO(b"video") if is_video else Image.new("RGB", (320, 240))), False

    await invoke_command(lobster.process_meme, message, meta=MetaInfo(message, text="мой кот"), cpu_executor=SimpleNamespace())

    assert downloaded.call_args.args[2] is (lobster.caption_video if is_video else lobster.caption_image)
    assert bot.session.methods[-1].reply_parameters.message_id == 11


async def test_profile_photo_fallback_keeps_replied_user_as_target(bot, actions, downloaded, monkeypatch):
    target = make_message(bot, message_id=10, text="это я")
    message = make_message(bot, text="/meme мой кот", reply_to_message=target)
    photo = PhotoSize(file_id="profile", file_unique_id="profile", width=320, height=240)
    profile = AsyncMock(return_value=photo)
    monkeypatch.setattr(SimpleExtractor, "profile_photo", profile)
    downloaded.return_value = Image.new("RGB", (320, 240)), False

    await invoke_command(lobster.process_meme, message, meta=MetaInfo(message, text="мой кот"), cpu_executor=SimpleNamespace())

    assert profile.call_args.args[0].message_id == 10
    assert downloaded.call_args.args[1] is photo
    assert bot.session.methods[-1].reply_parameters.message_id == 10


@pytest.mark.parametrize("kind", ["photo", "video"])
@pytest.mark.parametrize("failure", ["download", "layout", "timeout", "invalid"])
async def test_caption_failures_reply_without_upload_and_stop_chat_action(bot, actions, downloaded, kind, failure):
    message = make_message(bot, caption="/meme мой кот", **media_fields(kind))
    if failure == "download":
        downloaded.side_effect = DownloadUnavailable
    elif failure == "layout":
        downloaded.side_effect = CaptionLayoutError("Не получилось прочитать картинку.")
    else:
        downloaded.return_value = None, failure == "timeout"

    await invoke_command(lobster.process_meme, message, meta=MetaInfo(message, text="мой кот"), cpu_executor=SimpleNamespace())

    assert [method.__api_method__ for method in bot.session.methods] == ["sendMessage"]
    assert bot.session.methods[0].text
    assert actions[-1][0] == "stop"


@pytest.mark.parametrize("kind", ["photo", "video"])
async def test_upload_failure_still_closes_rendered_media_and_stops_chat_action(bot, actions, downloaded, monkeypatch, kind):
    message = make_message(bot, caption="/meme мой кот", **media_fields(kind))
    result = io.BytesIO(b"video") if kind == "video" else Image.new("RGB", (320, 240))
    downloaded.return_value = result, False
    monkeypatch.setattr(bot.session, "make_request", AsyncMock(side_effect=RuntimeError("synthetic upload failure")))

    with pytest.raises(ResponseDeliveryError) as failure:
        await invoke_command(lobster.process_meme, message, meta=MetaInfo(message, text="мой кот"), cpu_executor=SimpleNamespace())

    assert failure.value.uncertain
    if isinstance(result, io.BytesIO):
        assert result.closed
    else:
        with pytest.raises(ValueError):
            result.getpixel((0, 0))
    assert actions[-1][0] == "stop"


async def test_empty_caption_does_not_download_or_fetch_profile(bot, actions, downloaded, monkeypatch):
    message = make_message(bot, text="/meme")
    profile = AsyncMock()
    monkeypatch.setattr(SimpleExtractor, "profile_photo", profile)

    await invoke_command(lobster.process_meme, message, meta=MetaInfo(message), cpu_executor=SimpleNamespace())

    downloaded.assert_not_awaited()
    profile.assert_not_awaited()
    assert len(bot.session.methods) == 1
    assert "/meme" in bot.session.methods[0].text
    assert actions == []


def test_help_with_meme_fits_one_telegram_message():
    from html.parser import HTMLParser

    from msu_hub_bot.texts import cmd_help

    class VisibleText(HTMLParser):
        def __init__(self):
            super().__init__()
            self.parts = []

        def handle_data(self, data):
            self.parts.append(data)

    parser = VisibleText()
    parser.feed(cmd_help)
    visible = "".join(parser.parts)
    assert "/meme" in visible
    assert len(visible.encode("utf-16-le")) // 2 <= 4096
