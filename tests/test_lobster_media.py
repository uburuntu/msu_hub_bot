import io
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from msu_hub_bot.commands import lobster as lobster_module


@pytest.fixture
def lobster(monkeypatch):
    @asynccontextmanager
    async def no_chat_action(*args):
        yield

    monkeypatch.setattr(lobster_module, "ChatActioner", no_chat_action)
    return lobster_module


def photo_input(argument):
    source = io.BytesIO()
    Image.new("RGB", (2, 3)).save(source, "PNG")
    source.seek(0)
    target = SimpleNamespace(reply_photo=AsyncMock())
    meta = SimpleNamespace(
        arguments=[argument],
        extract_image_with_downloading=AsyncMock(return_value=(target, source)),
        extract_image=AsyncMock(return_value=(target, source)),
    )
    message = SimpleNamespace(reply=AsyncMock(), chat=SimpleNamespace(type="private"), bot=None)
    return message, meta, target


@pytest.mark.parametrize("handler", ["process_atmta", "process_atmta_v"])
@pytest.mark.parametrize("argument", ["0", "-1"])
async def test_zero_crop_explains_input_instead_of_sending_empty_image(lobster, handler, argument):
    message, meta, target = photo_input(argument)
    await getattr(lobster, handler)(message, meta)
    message.reply.assert_awaited_once()
    target.reply_photo.assert_not_awaited()


@pytest.mark.parametrize("handler, expected", [("process_atmta", (2, 3)), ("process_atmta_v", (2, 2))])
async def test_small_positive_crop_keeps_one_source_pixel(lobster, handler, expected):
    message, meta, target = photo_input("0.00001")
    await getattr(lobster, handler)(message, meta)
    result = target.reply_photo.call_args.args[0]
    assert Image.open(io.BytesIO(result.data)).size == expected
    message.reply.assert_not_awaited()
