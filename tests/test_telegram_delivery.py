import io

import pytest
from aiogram import Bot
from aiogram.types import BufferedInputFile, InputMediaDocument, InputMediaPhoto, ReplyParameters

from common.tg.delivery import ReplyTarget, reply_album, send_album
from common.tg.files import download_by_file_id, download_text, input_file
from telegram_helpers import RecordingSession, make_message


@pytest.mark.parametrize(
    "count,methods", [(0, []), (1, ["sendPhoto"]), (2, ["sendMediaGroup"]), (10, ["sendMediaGroup"]), (11, ["sendMediaGroup", "sendPhoto"])]
)
async def test_album_boundaries_keep_reply_topic_and_first_caption(count, methods):
    session = RecordingSession()
    bot = Bot("123456789:" + "a" * 35, session=session)
    message = make_message(bot, message_thread_id=17, is_topic_message=True)
    media = [InputMediaPhoto(media=f"file-{index}", caption="Caption" if index == 0 else None) for index in range(count)]
    sent = await reply_album(message, media)
    assert len(sent) == count
    assert [method.__api_method__ for method in session.methods] == methods
    for method in session.methods:
        assert method.message_thread_id == 17
        assert method.reply_parameters.message_id == message.message_id
    if count:
        first = session.methods[0]
        caption = first.media[0].caption if first.__api_method__ == "sendMediaGroup" else first.caption
        assert caption == "Caption"


async def test_single_document_preserves_filename_and_missing_reply_policy():
    session = RecordingSession()
    bot = Bot("123456789:" + "a" * 35, session=session)
    upload = input_file(b"test", "result.txt")
    media = [InputMediaDocument(media=upload, caption="plain", parse_mode=None)]
    reply = ReplyParameters(message_id=4, allow_sending_without_reply=True)
    await send_album(bot, 42, media, reply_parameters=reply)
    method = session.methods[0]
    assert method.__api_method__ == "sendDocument"
    assert method.document.filename == "result.txt"
    assert method.parse_mode is None
    assert method.reply_parameters.allow_sending_without_reply is True


def test_general_topic_does_not_invent_thread_identity():
    assert ReplyTarget.from_message(make_message()).thread_id is None


def test_buffer_upload_snapshots_content_independent_of_cursor():
    stream = io.BytesIO(b"complete")
    stream.seek(3)
    upload = input_file(stream, "result.bin")
    stream.close()
    assert isinstance(upload, BufferedInputFile)
    assert upload.data == b"complete"


async def test_download_rewinds_owned_stream_and_decodes_unicode():
    session = RecordingSession()
    session.download_bytes = "Привет 🐈".encode()
    bot = Bot("123456789:" + "a" * 35, session=session)
    stream = await download_by_file_id("file", bot)
    assert stream.tell() == 0
    assert stream.read() == session.download_bytes
    stream.close()
    assert await download_text("file", bot) == "Привет 🐈"
