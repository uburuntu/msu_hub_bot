from datetime import UTC, datetime

import pytest
from aiogram.methods import AnswerWebAppQuery, SendChatAction, SendMediaGroup, SendMessage
from aiogram.types import (
    BufferedInputFile,
    Chat,
    InlineQueryResultArticle,
    InputMediaPhoto,
    InputTextMessageContent,
    Message,
    ReplyKeyboardRemove,
    SentWebAppMessage,
)

from teleforge.testing import RecordingBot, RecordingSession


async def test_native_boolean_and_nonmessage_results_keep_their_real_shape() -> None:
    bot = RecordingBot()
    assert await bot(SendChatAction(chat_id=7, action="typing")) is True
    request = AnswerWebAppQuery(
        web_app_query_id="web-query",
        result=InlineQueryResultArticle(
            id="result", title="Result", input_message_content=InputTextMessageContent(message_text="done")
        ),
    )
    with pytest.raises(AssertionError, match="Configure an offline response"):
        await bot(request)
    expected = SentWebAppMessage(inline_message_id="inline-id")
    bot.recording.responses.append(expected)
    assert await bot(request) is expected


async def test_native_album_and_additional_upload_fields_are_fully_recorded() -> None:
    result = [Message(message_id=3, date=datetime.now(UTC), chat=Chat(id=7, type="private"))]
    bot = RecordingBot(session=RecordingSession([result, result[0]]))
    album = SendMediaGroup(chat_id=7, media=[InputMediaPhoto(media=BufferedInputFile(b"photo", "photo.jpg"))])
    assert await bot(album) is result
    assert bot.recording.uploads[0] == {"media.0.media": b"photo"}
    native = SendMessage(chat_id=7, text="text", future_attachment=BufferedInputFile(b"extra", "future.bin"))
    await bot(native)
    assert bot.requests == [album, native]
    assert bot.recording.uploads[1] == {"future_attachment": b"extra"}


async def test_reply_keyboard_removal_is_sent_but_not_returned_as_inline_markup() -> None:
    bot = RecordingBot()
    method = SendMessage(chat_id=7, text="Cancelled", reply_markup=ReplyKeyboardRemove())
    result = await bot(method)
    assert bot.requests == [method] and result.reply_markup is None
    assert isinstance(bot.requests[0].reply_markup, ReplyKeyboardRemove)
