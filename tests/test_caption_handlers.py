"""Caption commands retain media selection, reply policy and stream ownership."""

import io
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from PIL import Image
from aiogram.enums import ChatAction
from aiogram.types import PhotoSize, Update, UserProfilePhotos
from teleforge import App
from teleforge.delivery import DeliveryError

from msu_hub_bot.execution.executor import TPExecutor
from msu_hub_bot.features import captions
from msu_hub_bot.features.command import format_input_error
from msu_hub_bot.media.caption_layout import CaptionLayoutError
from msu_hub_bot.media.ffmpeg import MAX_OUTPUT_BYTES
from msu_hub_bot.telegram.media_jobs import DownloadUnavailable
from telegram_helpers import make_bot, make_message

STYLES = [("lobster", "lobster"), ("demotivator", "demotivator"), ("meme", "meme")]


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
async def dispatch():
    executor = TPExecutor(1)
    app = App(data={"cpu_executor": executor}, input_formatter=format_input_error).include(captions.Captions())

    async def invoke(message):
        return await app.feed_update(message.bot, Update(update_id=1, message=message))

    try:
        yield invoke
    finally:
        await app.aclose()
        executor.shutdown(wait=False)


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

    monkeypatch.setattr(captions, "ChatActioner", action)
    return recorded


@pytest.fixture
def downloaded(monkeypatch):
    call = AsyncMock()
    monkeypatch.setattr(captions, "run_downloaded", call)
    return call


@pytest.mark.parametrize("command,style", STYLES)
@pytest.mark.parametrize("private", [False, True])
async def test_image_caption_keeps_long_text_reply_topic_and_closes_rendered_image(
    bot, dispatch, actions, downloaded, command, style, private
):
    chat = {"id": 42, "type": "private"} if private else {"id": -1001234567890, "type": "supergroup"}
    topic = {} if private else {"is_topic_message": True, "message_thread_id": 55}
    target = make_message(bot, message_id=10, chat=chat, **topic, **media_fields("photo"))
    text = 'Привет: "[100%]" \\ путь. ' * 150
    message = make_message(bot, text=f"/{command} {text}", chat=chat, reply_to_message=target, **topic)
    result = Image.new("RGB", (320, 240), "red")
    downloaded.return_value = result, False

    await dispatch(message)

    assert downloaded.call_args.args[1].file_id == "source"
    assert downloaded.call_args.args[2:] == (captions.caption_image, text.strip(), style)
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


@pytest.mark.parametrize("command,style", STYLES)
@pytest.mark.parametrize("kind", ["video", "animation", "video_note", "video_sticker", "document"])
async def test_all_video_inputs_use_full_media_not_thumbnail(bot, dispatch, actions, downloaded, command, style, kind):
    target = make_message(bot, message_id=10, **media_fields(kind))
    text = "длинная подпись " * 150
    message = make_message(bot, text=f"/{command} {text}", reply_to_message=target)
    result = io.BytesIO(b"converted mp4")
    downloaded.return_value = result, False

    await dispatch(message)

    assert downloaded.call_args.args[1].file_id == "source"
    assert downloaded.call_args.args[2:] == (captions.caption_video, text.strip(), style)
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
async def test_attached_media_has_priority_over_reply_media(bot, dispatch, actions, downloaded, origin, reply):
    target = make_message(bot, message_id=10, **media_fields(reply))
    message = make_message(bot, message_id=11, caption="/meme мой кот", reply_to_message=target, **media_fields(origin))
    is_video = origin == "video"
    downloaded.return_value = (io.BytesIO(b"video") if is_video else Image.new("RGB", (320, 240))), False

    await dispatch(message)

    assert downloaded.call_args.args[2] is (captions.caption_video if is_video else captions.caption_image)
    assert bot.session.methods[-1].reply_parameters.message_id == 11


@pytest.mark.parametrize("attached_photo", [False, True])
async def test_mixed_rich_reply_prefers_video_but_attached_photo_still_wins(bot, dispatch, actions, downloaded, attached_photo):
    target = make_message(
        bot,
        message_id=10,
        rich_message={
            "blocks": [
                {
                    "type": "collage",
                    "blocks": [
                        {"type": "photo", **media_fields("photo")},
                        {"type": "video", **media_fields("video")},
                    ],
                }
            ]
        },
    )
    fields = {"caption": "/meme подпись", **media_fields("photo")} if attached_photo else {"text": "/meme подпись"}
    message = make_message(bot, message_id=11, reply_to_message=target, **fields)
    downloaded.return_value = (Image.new("RGB", (20, 20)) if attached_photo else io.BytesIO(b"converted video")), False

    await dispatch(message)

    assert downloaded.call_args.args[2] is (captions.caption_image if attached_photo else captions.caption_video)
    sent = bot.session.methods[-1]
    assert sent.__api_method__ == ("sendPhoto" if attached_photo else "sendVideo")
    assert sent.reply_parameters.message_id == (11 if attached_photo else 10)


async def test_full_native_video_output_limit_fits_with_group_controls(bot, dispatch, actions, downloaded):
    payload = b"v" * MAX_OUTPUT_BYTES
    output = io.BytesIO(payload)
    downloaded.return_value = output, False
    target = make_message(bot, message_id=10, is_topic_message=True, message_thread_id=55, **media_fields("video"))
    message = make_message(bot, text="/meme подпись", reply_to_message=target, is_topic_message=True, message_thread_id=55)

    await dispatch(message)

    assert [method.__api_method__ for method in bot.session.methods] == ["sendVideo"]
    sent = bot.session.methods[0]
    assert sent.video.data == payload
    assert sent.reply_markup is not None and sent.supports_streaming
    assert sent.reply_parameters.message_id == 10 and sent.message_thread_id == 55
    assert output.closed and actions[-1][0] == "stop"


async def test_profile_photo_fallback_keeps_replied_user_as_target(bot, dispatch, actions, downloaded, monkeypatch):
    target = make_message(bot, message_id=10, text="это я")
    message = make_message(bot, text="/meme мой кот", reply_to_message=target)
    photo = PhotoSize(file_id="profile", file_unique_id="profile", width=320, height=240)
    profile = AsyncMock(return_value=UserProfilePhotos(total_count=1, photos=[[photo]]))
    monkeypatch.setattr(bot, "get_user_profile_photos", profile)
    downloaded.return_value = Image.new("RGB", (320, 240)), False

    await dispatch(message)

    assert profile.call_args.args[0] == target.from_user.id
    assert downloaded.call_args.args[1] is photo
    assert bot.session.methods[-1].reply_parameters.message_id == 10


@pytest.mark.parametrize("kind", ["photo", "video"])
@pytest.mark.parametrize("failure", ["download", "layout", "timeout", "invalid"])
async def test_caption_failures_reply_without_upload_and_stop_chat_action(bot, dispatch, actions, downloaded, kind, failure):
    message = make_message(bot, caption="/meme мой кот", **media_fields(kind))
    if failure == "download":
        downloaded.side_effect = DownloadUnavailable
    elif failure == "layout":
        downloaded.side_effect = CaptionLayoutError("Не получилось прочитать картинку.")
    else:
        downloaded.return_value = None, failure == "timeout"

    await dispatch(message)

    assert [method.__api_method__ for method in bot.session.methods] == ["sendMessage"]
    assert bot.session.methods[0].text
    assert actions[-1][0] == "stop"


@pytest.mark.parametrize("kind", ["photo", "video"])
async def test_upload_failure_still_closes_rendered_media_and_stops_chat_action(bot, dispatch, actions, downloaded, monkeypatch, kind):
    message = make_message(bot, caption="/meme мой кот", **media_fields(kind))
    result = io.BytesIO(b"video") if kind == "video" else Image.new("RGB", (320, 240))
    downloaded.return_value = result, False
    monkeypatch.setattr(bot.session, "make_request", AsyncMock(side_effect=RuntimeError("synthetic upload failure")))

    with pytest.raises(DeliveryError) as error:
        await dispatch(message)
    assert str(error.value.cause) == "synthetic upload failure"

    if isinstance(result, io.BytesIO):
        assert result.closed
    else:
        with pytest.raises(ValueError):
            result.getpixel((0, 0))
    assert actions[-1][0] == "stop"


async def test_empty_caption_guides_without_downloading_or_fetching_profile(bot, dispatch, actions, downloaded, monkeypatch):
    message = make_message(bot, text="/meme")
    profile = AsyncMock()
    monkeypatch.setattr(bot, "get_user_profile_photos", profile)

    await dispatch(message)

    downloaded.assert_not_awaited()
    profile.assert_not_awaited()
    assert [method.__api_method__ for method in bot.session.methods] == ["sendMessage"]
    assert bot.session.methods[0].text == "Добавь текст после команды или ответь ею на сообщение с текстом."
    assert actions == []


@pytest.mark.parametrize(
    "text,style",
    [
        ("/l подпись", "lobster"),
        ("/л подпись", "lobster"),
        ("подпись #ЛОБСТЕР", "lobster"),
        ("/de подпись", "demotivator"),
        ("/д подпись", "demotivator"),
        ("подпись #ДЕ", "demotivator"),
    ],
)
async def test_aliases_keep_their_style_and_hashtag_body(bot, dispatch, actions, downloaded, text, style):
    message = make_message(bot, caption=text, **media_fields("photo"))
    downloaded.return_value = Image.new("RGB", (20, 20)), False

    await dispatch(message)

    assert downloaded.call_args.args[3:] == ("подпись", style)
    assert bot.session.methods[-1].photo.filename == f"{style}.png"


@pytest.mark.parametrize(
    "text,style,caption",
    [
        ("подпись #meme #lobster", "lobster", "подпись #meme"),
        ("подпись #meme #demotivator", "demotivator", "подпись #meme"),
        ("/meme подпись #lobster", "lobster", "/meme подпись"),
        ("/demotivator подпись #l", "lobster", "/demotivator подпись"),
        ("/lobster подпись #meme", "lobster", "подпись #meme"),
        ("подпись #de #l", "lobster", "подпись #de"),
    ],
)
async def test_multiple_triggers_preserve_style_route_precedence(bot, dispatch, actions, downloaded, text, style, caption):
    message = make_message(bot, caption=text, **media_fields("photo"))
    downloaded.return_value = Image.new("RGB", (20, 20)), False

    await dispatch(message)

    assert downloaded.call_args.args[3:] == (caption, style)
    assert bot.session.methods[-1].photo.filename == f"{style}.png"


@pytest.mark.parametrize("original_has_photo", [False, True])
async def test_forwarded_avatar_prefers_original_author_then_forwarder(bot, dispatch, actions, downloaded, monkeypatch, original_has_photo):
    target = make_message(
        bot,
        message_id=10,
        text="пересланный текст",
        from_user={"id": 8, "is_bot": False, "first_name": "Forwarder"},
        forward_origin={"type": "user", "date": 1, "sender_user": {"id": 9, "is_bot": False, "first_name": "Original"}},
    )
    message = make_message(bot, text="/meme подпись", reply_to_message=target)

    async def photos(user_id, **kwargs):
        found = original_has_photo or user_id != 9
        return UserProfilePhotos(
            total_count=int(found),
            photos=[[PhotoSize(file_id=f"avatar-{user_id}", file_unique_id="avatar", width=20, height=20)]] if found else [],
        )

    profile = AsyncMock(side_effect=photos)
    monkeypatch.setattr(bot, "get_user_profile_photos", profile)
    downloaded.return_value = Image.new("RGB", (20, 20)), False

    await dispatch(message)

    assert [call.args[0] for call in profile.await_args_list] == ([9] if original_has_photo else [9, 8])
    assert downloaded.call_args.args[1].file_id == f"avatar-{9 if original_has_photo else 8}"
    assert bot.session.methods[-1].reply_parameters.message_id == 10


async def test_nonraster_document_does_not_displace_usable_reply_photo(bot, dispatch, actions, downloaded):
    target = make_message(bot, message_id=10, **media_fields("photo"))
    message = make_message(
        bot,
        caption="/meme подпись",
        reply_to_message=target,
        document={"file_id": "svg", "file_unique_id": "svg", "mime_type": "image/svg+xml", "file_name": "drawing.svg"},
    )
    downloaded.return_value = Image.new("RGB", (20, 20)), False

    await dispatch(message)

    assert downloaded.call_args.args[1].file_id == "source"
    assert bot.session.methods[-1].reply_parameters.message_id == 10


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
