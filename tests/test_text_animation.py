import gzip
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from msu_hub_bot.commands import animate


@pytest.fixture
def animation():
    return animate


def paths(value):
    if isinstance(value, dict):
        if value.get("ty") == "sh":
            yield value
        for child in value.values():
            yield from paths(child)
    elif isinstance(value, list):
        for child in value:
            yield from paths(child)


@pytest.mark.parametrize("builder", ["AnimateTextSticker", "MatrixSticker"])
@pytest.mark.parametrize("text", ["Hello", "Привет", "Ёж й", "A B"])
def test_latin_cyrillic_and_composite_glyphs_generate_valid_stickers(animation, builder, text):
    result = animation.animate(getattr(animation, builder), text)
    assert result is not None
    payload = result.getvalue()
    assert len(payload) < 64 * 1024
    document = json.loads(gzip.decompress(payload))
    assert document["w"] == document["h"] == 512
    assert 0 < (document["op"] - document["ip"]) / document["fr"] <= 3
    assert list(paths(document)), "The result must contain rendered outlines, not just an empty animation."


def test_spaces_and_missing_characters_do_not_crash(animation):
    assert animation.animate(animation.AnimateTextSticker, "   ") is None
    assert animation.animate(animation.AnimateTextSticker, "\U0001f642") is None


async def test_animate_handler_delivers_generated_sticker(animation):
    async def execute(func, *args):
        return func(*args), False

    worker = SimpleNamespace(run=AsyncMock(side_effect=execute))
    target = SimpleNamespace(reply_sticker=AsyncMock())
    message = SimpleNamespace(reply=AsyncMock())
    meta = SimpleNamespace(extract_text=lambda: (target, "Привет"))
    await animation.process_animate(message, meta, worker)
    target.reply_sticker.assert_awaited_once()
    message.reply.assert_not_awaited()
