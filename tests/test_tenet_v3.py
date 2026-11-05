"""Reverse commands preserve their playful content through native v3 requests."""

import io
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image
from aiogram.methods import AddStickerToSet, DeleteStickerFromSet, SendDocument, SendPhoto, SendPoll, SendSticker, SendVoice
from aiogram.types import BufferedInputFile, PhotoSize, Sticker, UserProfilePhotos

from msu_hub_bot.commands import tenet
from msu_hub_bot.execution.executor import TPExecutor
from msu_hub_bot.media import limits
from telegram_helpers import make_bot, make_message


def worker(payload=b"converted"):
    async def prepare_job(prepare, func, **kwargs):
        await prepare()
        return io.BytesIO(payload), False

    return SimpleNamespace(run=AsyncMock(return_value=(io.BytesIO(payload), False)), run_prepared=AsyncMock(side_effect=prepare_job))


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
    executor.run_prepared.assert_awaited_once()


async def test_image_document_without_filename_is_mirrored():
    bot = make_bot()
    source = Image.new("RGB", (2, 1), "red")
    source.putpixel((1, 0), (0, 0, 255))
    buffer = io.BytesIO()
    source.save(buffer, format="PNG")
    bot.session.download_bytes = buffer.getvalue()
    target = make_message(bot, document=dict(file_id="image", file_unique_id="image-id", mime_type="image/png"))
    executor = TPExecutor(1)
    try:
        await tenet.process_reverse(make_message(bot, reply_to_message=target), bot, executor)
    finally:
        executor.shutdown(wait=True)
    method = bot.session.methods[-1]
    assert isinstance(method, SendDocument)
    assert method.document.filename == "egami.png"
    result = Image.open(io.BytesIO(method.document.data))
    assert result.getpixel((0, 0)) == (0, 0, 255)
    assert result.getpixel((1, 0)) == (255, 0, 0)


async def test_video_sticker_is_sent_directly_without_a_temporary_pack(monkeypatch):
    bot = make_bot()
    sticker = Sticker(
        file_id="original", file_unique_id="identity", type="regular", width=512, height=512, is_animated=False, is_video=True
    )
    target = make_message(bot, sticker=sticker)
    await tenet.process_reverse(make_message(bot, reply_to_message=target), bot, worker())
    methods = bot.session.methods
    assert not any(isinstance(method, (AddStickerToSet, DeleteStickerFromSet)) for method in methods)
    sent = methods[-1]
    assert isinstance(sent, SendSticker) and sent.sticker.data == b"converted" and sent.sticker.filename == "sticker.webm"


async def test_reverse_budget_failure_explains_the_actual_limit(monkeypatch):
    bot = make_bot()
    target = make_message(bot, voice=dict(file_id="voice", file_unique_id="voice-id", duration=3))
    explanation = "Файл слишком большой для разворота. Попробуй более короткий фрагмент или меньшее разрешение."
    monkeypatch.setattr(tenet, "run_downloaded", AsyncMock(side_effect=tenet.ReverseMediaError(explanation)))
    await tenet.process_reverse(make_message(bot, reply_to_message=target), bot, worker())
    assert bot.session.methods[-1].text == explanation


@pytest.mark.parametrize("kind", ["avatar", "photo", "sticker"])
async def test_mirror_jobs_keep_the_original_telegram_media_type(monkeypatch, kind):
    bot = make_bot()
    buffer = io.BytesIO()
    with Image.new("RGB", (2, 1), "red") as source:
        source.save(buffer, format="PNG")
    bot.session.download_bytes = buffer.getvalue()
    photo = PhotoSize(file_id="image", file_unique_id="image-id", width=2, height=1)
    if kind == "avatar":
        monkeypatch.setattr(bot, "get_user_profile_photos", AsyncMock(return_value=UserProfilePhotos(total_count=1, photos=[[photo]])))
        message = make_message(bot)
    elif kind == "photo":
        message = make_message(bot, reply_to_message=make_message(bot, photo=[photo]))
    else:
        sticker = Sticker(file_id="image", file_unique_id="image-id", type="regular", width=2, height=1, is_animated=False, is_video=False)
        message = make_message(bot, reply_to_message=make_message(bot, sticker=sticker))
    executor = TPExecutor(1)
    try:
        await tenet.process_reverse(message, bot, executor)
    finally:
        executor.shutdown(wait=True)
    method = bot.session.methods[-1]
    assert isinstance(method, SendSticker if kind == "sticker" else SendPhoto)
    prepared = method.sticker if kind == "sticker" else method.photo
    with Image.open(io.BytesIO(prepared.data)) as image:
        assert image.size == (2, 1)


def test_oversized_mirror_is_rejected_before_decoding(monkeypatch):
    buffer = io.BytesIO()
    with Image.new("RGB", (2, 2), "red") as source:
        source.save(buffer, format="PNG")
    buffer.seek(0)
    monkeypatch.setattr(limits, "MAX_IMAGE_PIXELS", 3)

    def decode(*args, **kwargs):
        pytest.fail("mirror decoded an image before checking dimensions")

    monkeypatch.setattr(Image.Image, "load", decode)
    with pytest.raises(limits.MediaDimensionsError):
        tenet.mirror_image(buffer)
