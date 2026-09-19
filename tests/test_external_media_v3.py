"""Provider results enter real v3 upload/album models without network access."""

import io
from unittest.mock import AsyncMock

import pytest
from aiogram.methods import SendDocument, SendMediaGroup, SendMessage, SendVideo
from aiogram.types import BufferedInputFile, URLInputFile
from msu_hub_bot.providers.pdf import PdfDocument

from msu_hub_bot.telegram.filters import MetaInfo
from msu_hub_bot.commands import externals
from telegram_helpers import make_bot, make_message


@pytest.mark.parametrize("count", [0, 1, 3])
async def test_anime_results_use_native_delivery_and_keep_topic(monkeypatch, count):
    bot = make_bot()
    message = make_message(bot, is_topic_message=True, message_thread_id=9)
    monkeypatch.setattr(externals, "extract_image", AsyncMock(return_value=(message, None)))
    monkeypatch.setattr(externals, "download", AsyncMock(return_value=io.BytesIO(b"image")))
    monkeypatch.setattr(
        externals,
        "which_anime",
        AsyncMock(
            return_value={
                "result": [
                    dict(filename=f"Episode <{index}>", anilist=index, similarity=0.95, video=f"https://example.org/{index}.mp4")
                    for index in range(count)
                ]
            }
        ),
    )
    await externals.process_which_anime(message)
    method = bot.session.methods[-1]
    assert method.message_thread_id == 9 and method.reply_parameters.message_id == message.message_id
    if not count:
        assert isinstance(method, SendMessage)
    elif count == 1:
        assert isinstance(method, SendVideo) and isinstance(method.video, URLInputFile)
        assert method.video.filename == "0.mp4" and "&lt;0&gt;" in method.caption
    else:
        assert isinstance(method, SendMediaGroup) and len(method.media) == count
        assert method.media[0].caption and not any(item.caption for item in method.media[1:])
        assert all(isinstance(item.media, URLInputFile) for item in method.media)


async def test_background_uses_bounded_local_worker_and_keeps_topic(monkeypatch):
    bot = make_bot()
    message = make_message(bot, is_topic_message=True, message_thread_id=9)
    executor, media = object(), object()
    monkeypatch.setattr(externals, "extract_image", AsyncMock(return_value=(message, media)))
    worker = AsyncMock(return_value=(b"synthetic-png", False))
    monkeypatch.setattr(externals, "run_downloaded", worker)
    await externals.process_bg(message, executor)
    worker.assert_awaited_once_with(executor, media, externals.remove_background, bot=bot)
    method = bot.session.methods[-1]
    assert isinstance(method, SendDocument) and method.message_thread_id == 9
    assert method.document.data == b"synthetic-png" and method.document.filename.endswith(".png")


async def test_pdf_delivers_downloaded_bytes_with_the_document_filename(monkeypatch):
    bot = make_bot()
    message = make_message(bot, document=dict(file_id="document", file_unique_id="id", file_name="input.txt", mime_type="text/plain"))
    monkeypatch.setattr(
        externals,
        "convert_to_pdf",
        AsyncMock(return_value=PdfDocument(b"%PDF-synthetic", filename="input.pdf")),
    )
    await externals.process_topdf(message, MetaInfo(message))
    method = bot.session.methods[-1]
    assert isinstance(method, SendDocument)
    assert isinstance(method.document, BufferedInputFile) and method.document.filename == "input.pdf"
    assert method.document.data == b"%PDF-synthetic"
    assert method.thumbnail is None
