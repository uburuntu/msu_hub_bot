import io
import json
import shutil
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from hub_bot.commands import lobster as lobster_module
from hub_bot.utils import caption_layout


@pytest.fixture
def lobster(monkeypatch):
    @asynccontextmanager
    async def no_chat_action(*args):
        yield

    monkeypatch.setattr(lobster_module, "ChatActioner", no_chat_action)
    monkeypatch.setattr(lobster_module, "download", AsyncMock(return_value=io.BytesIO(b"video")))
    return lobster_module


@pytest.fixture
def worker():
    return SimpleNamespace(run=AsyncMock())


def photo_input(argument):
    source = io.BytesIO()
    Image.new("RGB", (2, 3)).save(source, "PNG")
    source.seek(0)
    target = SimpleNamespace(reply_photo=AsyncMock())
    meta = SimpleNamespace(arguments=[argument], extract_image_with_downloading=AsyncMock(return_value=(target, source)))
    message = SimpleNamespace(reply=AsyncMock(), chat=SimpleNamespace(type="private"))
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


@pytest.mark.parametrize("timeout", [False, True])
async def test_video_conversion_failure_replies_without_sending_none(lobster, worker, timeout):
    worker.run.return_value = None, timeout
    target = SimpleNamespace(reply_video=AsyncMock())
    video = SimpleNamespace(file_size=100, width=32, download=AsyncMock(return_value=io.BytesIO(b"invalid video")))
    meta = SimpleNamespace(extract_video=AsyncMock(return_value=(target, video)), extract_text=lambda: (target, "Привет"))
    message = SimpleNamespace(reply=AsyncMock(), chat=SimpleNamespace(type="private"))
    await lobster.process_demotivator_video(message, meta, worker)
    message.reply.assert_awaited_once()
    target.reply_video.assert_not_awaited()


@pytest.mark.parametrize("metadata", [{}, {"file_size": None}])
async def test_video_without_size_metadata_reaches_conversion(lobster, worker, metadata):
    result = io.BytesIO(b"converted video")
    worker.run.return_value = result, False
    target = SimpleNamespace(reply_video=AsyncMock())
    video = SimpleNamespace(**metadata, width=None, download=AsyncMock(return_value=io.BytesIO(b"video")))
    meta = SimpleNamespace(extract_video=AsyncMock(return_value=(target, video)), extract_text=lambda: (target, "Привет"))
    message = SimpleNamespace(reply=AsyncMock(), chat=SimpleNamespace(type="private"))
    await lobster.process_demotivator_video(message, meta, worker)
    assert target.reply_video.call_args.args[0].data == result.getvalue()
    assert target.reply_video.call_args.kwargs == {"reply_markup": None}
    message.reply.assert_not_awaited()


@pytest.mark.parametrize("handler", ["process_lobster", "process_demotivator"])
async def test_caption_length_error_is_a_reply_not_a_truncated_image(lobster, worker, handler):
    async def execute(func, *args):
        return func(*args), False

    worker.run.side_effect = execute
    message, meta, target = photo_input("0.5")
    meta.extract_text = lambda: (target, "x" * 1025)
    meta.extract_video = AsyncMock(return_value=(target, None))
    await getattr(lobster, handler)(message, meta, worker)
    assert "1024" in message.reply.call_args.args[0]
    target.reply_photo.assert_not_awaited()


def test_video_caption_never_enters_filter_syntax_and_temp_file_is_removed(lobster, monkeypatch):
    text = 'Привет: "it\'s fine"; [100%], \\path\\.'
    paths = []

    def convert(file, parameters, out_suffix):
        assert text not in " ".join(parameters)
        path = Path(parameters[parameters.index("-i") + 1])
        paths.append(path)
        assert Image.open(path).getbbox() is not None
        return None

    monkeypatch.setattr(lobster, "ffmpeg", convert)
    assert lobster.demotivator_video(io.BytesIO(b"input"), 320, text) is None
    assert not paths[0].parent.exists()


@pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="FFmpeg/FFprobe are not installed")
@pytest.mark.parametrize("text", ['Привет: "it\'s fine"; 100% [ready], \\path\\.\nЕщё строка 🙂', "WideW" * 30])
def test_native_video_keeps_complete_caption_audio_and_final_frames(lobster, tmp_path, text):
    source = tmp_path / "source.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x240:rate=10:duration=0.8",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=0.8",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(source),
        ],
        check=True,
        capture_output=True,
        timeout=20,
    )
    result = lobster.demotivator_video(io.BytesIO(source.read_bytes()), 320, text)
    assert result is not None
    output = tmp_path / "result.mp4"
    output.write_bytes(result.getvalue())
    info = json.loads(
        subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(output)],
            check=True,
            capture_output=True,
            timeout=10,
        ).stdout
    )
    video = next(stream for stream in info["streams"] if stream["codec_type"] == "video")
    assert any(stream["codec_type"] == "audio" for stream in info["streams"])
    assert int(video["nb_frames"]) == 8
    assert video["width"] % 2 == video["height"] % 2 == 0
    border = caption_layout.frame_border(320)
    panel = caption_layout.demotivator_caption(video["width"], text, border)
    for timestamp in ("0", "0.6"):
        frame = subprocess.run(
            ["ffmpeg", "-v", "error", "-ss", timestamp, "-i", str(output), "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"],
            check=True,
            capture_output=True,
            timeout=10,
        ).stdout
        image = Image.open(io.BytesIO(frame)).convert("RGB")
        caption = image.crop((0, image.height - panel.height, image.width, image.height))
        bounds = caption.convert("L").point(lambda value: 255 if value > 100 else 0).getbbox()
        assert bounds is not None
        left, top, right, bottom = bounds
        assert border - 2 <= left < right <= image.width - border + 2
        assert border - 2 <= top < bottom <= panel.height - border + 2
