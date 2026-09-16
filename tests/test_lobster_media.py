import importlib.util
import io
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from hub_bot import resources
from hub_bot.utils import ffmpeg


@pytest.fixture
def lobster(monkeypatch):
    monkeypatch.setitem(sys.modules, 'app', SimpleNamespace(cpu_executor=SimpleNamespace(run=AsyncMock())))
    monkeypatch.setitem(sys.modules, 'resources', resources)
    monkeypatch.setitem(sys.modules, 'utils.ffmpeg', ffmpeg)
    spec = importlib.util.spec_from_file_location('lobster_media_test', Path(__file__).resolve().parents[1] / 'hub_bot/commands/lobster.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    @asynccontextmanager
    async def no_chat_action(*args):
        yield

    module.ChatActioner = no_chat_action
    return module


def photo_input(argument):
    source = io.BytesIO()
    Image.new('RGB', (2, 3)).save(source, 'PNG')
    source.seek(0)
    target = SimpleNamespace(reply_photo=AsyncMock())
    meta = SimpleNamespace(arguments=[argument], extract_image_with_downloading=AsyncMock(return_value=(target, source)))
    message = SimpleNamespace(reply=AsyncMock(), chat=SimpleNamespace(type='private'))
    return message, meta, target


@pytest.mark.parametrize('handler', ['process_atmta', 'process_atmta_v'])
@pytest.mark.parametrize('argument', ['0', '-1'])
async def test_zero_crop_explains_input_instead_of_sending_empty_image(lobster, handler, argument):
    message, meta, target = photo_input(argument)
    await getattr(lobster, handler)(message, meta)
    message.reply.assert_awaited_once()
    target.reply_photo.assert_not_awaited()


@pytest.mark.parametrize('handler, expected', [('process_atmta', (2, 3)), ('process_atmta_v', (2, 2))])
async def test_small_positive_crop_keeps_one_source_pixel(lobster, handler, expected):
    message, meta, target = photo_input('0.00001')
    await getattr(lobster, handler)(message, meta)
    result = target.reply_photo.call_args.args[0]
    assert Image.open(result).size == expected
    message.reply.assert_not_awaited()


@pytest.mark.parametrize('timeout', [False, True])
async def test_video_conversion_failure_replies_without_sending_none(lobster, timeout):
    lobster.cpu_executor.run.return_value = None, timeout
    target = SimpleNamespace(reply_video=AsyncMock())
    video = SimpleNamespace(file_size=100, width=32, download=AsyncMock(return_value=io.BytesIO(b'invalid video')))
    meta = SimpleNamespace(extract_video=AsyncMock(return_value=(target, video)), extract_text=lambda: (target, 'Привет'))
    message = SimpleNamespace(reply=AsyncMock(), chat=SimpleNamespace(type='private'))
    await lobster.process_demotivator_video(message, meta)
    message.reply.assert_awaited_once()
    target.reply_video.assert_not_awaited()
