"""Real bot services exercised through TeleForge's native update dispatch."""

import io
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from PIL import Image
from aiogram.types import Chat, Message, Update, User
from teleforge import App
from teleforge.testing import RecordingBot

from msu_hub_bot.features import captions
from msu_hub_bot.execution.executor import TPExecutor


def message(bot, *, text=None, message_id=1, actor=7, topic=55, **kwargs):
    return Message(
        message_id=message_id,
        date=datetime.now(UTC),
        chat=Chat(id=-100123, type="supergroup"),
        from_user=User(id=actor, is_bot=actor == bot.id, first_name="Tester"),
        text=text,
        message_thread_id=topic,
        is_topic_message=topic is not None,
        **kwargs,
    ).as_(bot)


@pytest.mark.parametrize("kind", ["photo", "video", "video_sticker"])
async def test_caption_sources_delivery_and_resource_ownership(monkeypatch, kind):
    bot = RecordingBot()
    base = {"file_id": "original", "file_unique_id": "file", "width": 24, "height": 16}
    if kind == "photo":
        fields = {"photo": [base]}
        output = Image.new("RGB", (24, 16), "red")
    elif kind == "video":
        fields = {"video": {**base, "duration": 1}}
        output = io.BytesIO(b"rendered video")
    else:
        fields = {"sticker": {**base, "is_video": True, "is_animated": False, "type": "regular"}}
        output = io.BytesIO(b"rendered video")
    renderer = AsyncMock(return_value=(output, False))
    monkeypatch.setattr(captions, "run_downloaded", renderer)

    @asynccontextmanager
    async def action(*args):
        yield

    monkeypatch.setattr(captions, "ChatActioner", action)
    source = message(bot, message_id=8, **fields)
    executor = TPExecutor(1)
    app = App(data={"cpu_executor": executor}).include(captions.Captions())
    try:
        await app.feed_update(bot, Update(update_id=1, message=message(bot, text="/meme длинная подпись", reply_to_message=source)))
        call = renderer.call_args.args
        assert call[1].file_id == "original" and call[3] == "длинная подпись"
        assert call[2] is (captions.caption_image if kind == "photo" else captions.caption_video)
        sent = bot.requests[-1]
        assert sent.__api_method__ == ("sendPhoto" if kind == "photo" else "sendVideo")
        assert sent.reply_parameters.message_id == 8 and sent.message_thread_id == 55
        assert bot.recording.uploads[-1]
        if kind == "photo":
            with pytest.raises(ValueError):
                output.getpixel((0, 0))
        else:
            assert output.closed
    finally:
        await app.aclose()
        executor.shutdown(wait=False)
