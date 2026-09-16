import gzip
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hub_bot import resources


@pytest.fixture
def animation(monkeypatch):
    monkeypatch.setitem(__import__('sys').modules, 'app', SimpleNamespace(cpu_executor=SimpleNamespace(run=AsyncMock())))
    monkeypatch.setitem(__import__('sys').modules, 'resources', resources)
    spec = importlib.util.spec_from_file_location('text_animation_test', Path(__file__).resolve().parents[1] / 'hub_bot/commands/animate.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def paths(value):
    if isinstance(value, dict):
        if value.get('ty') == 'sh':
            yield value
        for child in value.values():
            yield from paths(child)
    elif isinstance(value, list):
        for child in value:
            yield from paths(child)


@pytest.mark.parametrize('builder', ['AnimateTextSticker', 'MatrixSticker'])
@pytest.mark.parametrize('text', ['Hello', 'Привет', 'Ёж й', 'A B'])
def test_latin_cyrillic_and_composite_glyphs_generate_valid_stickers(animation, builder, text):
    result = animation.animate(getattr(animation, builder), text)
    assert result is not None
    payload = result.getvalue()
    assert len(payload) < 64 * 1024
    document = json.loads(gzip.decompress(payload))
    assert document['w'] == document['h'] == 512
    assert 0 < (document['op'] - document['ip']) / document['fr'] <= 3
    assert list(paths(document)), 'The result must contain rendered outlines, not just an empty animation.'


def test_spaces_and_missing_characters_do_not_crash(animation):
    assert animation.animate(animation.AnimateTextSticker, '   ') is None
    assert animation.animate(animation.AnimateTextSticker, '\U0001f642') is None


async def test_animate_handler_delivers_generated_sticker(animation):
    async def execute(func, *args):
        return func(*args), False

    animation.cpu_executor.run.side_effect = execute
    target = SimpleNamespace(reply_sticker=AsyncMock())
    message = SimpleNamespace(reply=AsyncMock())
    meta = SimpleNamespace(extract_text=lambda: (target, 'Привет'))
    await animation.process_animate(message, meta)
    target.reply_sticker.assert_awaited_once()
    message.reply.assert_not_awaited()
