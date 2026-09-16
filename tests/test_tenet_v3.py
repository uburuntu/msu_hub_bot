"""Reverse commands preserve their playful content through native v3 requests."""

import io
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image
from aiogram.methods import AddStickerToSet, DeleteStickerFromSet, SendDocument, SendPoll, SendSticker, SendVoice
from aiogram.types import BufferedInputFile, Sticker, StickerSet

from hub_bot.commands import tenet
from telegram_helpers import make_bot, make_message


def worker(payload=b"converted"):
    return SimpleNamespace(run=AsyncMock(return_value=(io.BytesIO(payload), False)))


@pytest.mark.parametrize("kind", ["quiz", "regular"])
async def test_reversed_poll_preserves_first_quiz_answer_and_reply_topic(kind):
    bot = make_bot()
    poll = dict(
        id="poll",
        question="Почему?",
        options=[dict(text="Один", voter_count=1, persistent_id="0"), dict(text="Два", voter_count=2, persistent_id="1")],
        total_voter_count=3,
        is_closed=False,
        is_anonymous=True,
        type=kind,
        allows_multiple_answers=False,
        allows_revoting=False,
        members_only=False,
    )
    target = make_message(bot, message_id=7, is_topic_message=True, message_thread_id=9, poll=poll)
    await tenet.process_reverse(make_message(bot, reply_to_message=target), bot, worker())
    method = bot.session.methods[-1]
    assert isinstance(method, SendPoll)
    assert method.question == "Почему?"[::-1]
    assert [option.text for option in method.options] == ["нидО", "авД"]
    assert method.correct_option_ids == ([0] if kind == "quiz" else None)
    assert method.reply_parameters.message_id == 7 and method.message_thread_id == 9


async def test_voice_without_size_metadata_uploads_converted_audio():
    bot = make_bot()
    target = make_message(bot, voice=dict(file_id="voice", file_unique_id="voice-id", duration=3), caption="<Привет>")
    executor = worker()
    await tenet.process_reverse(make_message(bot, reply_to_message=target), bot, executor)
    method = bot.session.methods[-1]
    assert isinstance(method, SendVoice)
    assert isinstance(method.voice, BufferedInputFile)
    assert method.voice.data == b"converted" and method.voice.filename == "audio.ogg"
    assert method.caption == "&gt;тевирП&lt;" and method.duration == 3
    assert executor.run.call_args.args[0] is tenet.reverse_audio


async def test_image_document_without_filename_is_mirrored():
    bot = make_bot()
    source = Image.new("RGB", (2, 1), "red")
    source.putpixel((1, 0), (0, 0, 255))
    buffer = io.BytesIO()
    source.save(buffer, format="PNG")
    bot.session.download_bytes = buffer.getvalue()
    target = make_message(bot, document=dict(file_id="image", file_unique_id="image-id", mime_type="image/png"))
    await tenet.process_reverse(make_message(bot, reply_to_message=target), bot, worker())
    method = bot.session.methods[-1]
    assert isinstance(method, SendDocument)
    assert method.document.filename == "egami.png"
    result = Image.open(io.BytesIO(method.document.data))
    assert result.getpixel((0, 0)) == (0, 0, 255)
    assert result.getpixel((1, 0)) == (255, 0, 0)


async def test_video_sticker_uses_native_add_preview_delete(monkeypatch):
    bot = make_bot()
    sticker = Sticker(
        file_id="original", file_unique_id="identity", type="regular", width=512, height=512, is_animated=False, is_video=True
    )
    saved = sticker.model_copy(update={"file_id": "saved"})
    monkeypatch.setattr(
        bot, "get_sticker_set", AsyncMock(return_value=StickerSet(name="pack", title="Pack", sticker_type="regular", stickers=[saved]))
    )
    target = make_message(bot, sticker=sticker)
    await tenet.process_reverse(make_message(bot, reply_to_message=target), bot, worker())
    methods = bot.session.methods
    add = next(method for method in methods if isinstance(method, AddStickerToSet))
    assert add.sticker.format == "video" and add.sticker.emoji_list == ["🔄"]
    assert add.sticker.sticker.data == b"converted"
    sent, removed = methods[-2:]
    assert isinstance(sent, SendSticker) and sent.sticker == "saved"
    assert isinstance(removed, DeleteStickerFromSet) and removed.sticker == "saved"
