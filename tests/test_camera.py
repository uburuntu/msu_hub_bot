"""Camera snapshots stay bounded while preserving chat and cache behavior."""

import io
import shutil
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram.methods import AnswerCallbackQuery, EditMessageReplyMarkup, SendMessage, SendPhoto
from aiogram.types import BufferedInputFile, CallbackQuery
from cachetools import TTLCache
from PIL import Image

from msu_hub_bot.commands import camera
from msu_hub_bot.execution.executor import TPExecutor
from telegram_helpers import make_bot, make_message


def jpeg(color: tuple[int, int, int]) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (32, 24), color).save(output, format="JPEG")
    return output.getvalue()


def assert_red_jpeg(content: bytes | None) -> None:
    assert content is not None
    with Image.open(io.BytesIO(content)) as image:
        assert image.format == "JPEG"
        assert image.size == (32, 24)
        red, green, blue = image.convert("RGB").getpixel((16, 12))
        assert red > 180 and green < 60 and blue < 70


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required for native media checks")
def test_native_mjpeg_fixture_uses_first_frame(tmp_path: Path) -> None:
    source = tmp_path / "camera.mjpeg"
    source.write_bytes(jpeg((220, 30, 40)) + jpeg((20, 30, 220)))
    assert_red_jpeg(camera.camera_frame(str(source)))


@pytest.mark.allow_hosts(["127.0.0.1"])
@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required for native media checks")
def test_native_http_mjpeg_stream_uses_first_frame() -> None:
    first = jpeg((220, 30, 40))
    subsequent = jpeg((20, 30, 220))

    class CameraHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.end_headers()
            try:
                for frame in [first, *([subsequent] * 11)]:
                    self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(frame)).encode() + b"\r\n\r\n")
                    self.wfile.write(frame + b"\r\n")
                    self.wfile.flush()
                self.wfile.write(b"--frame--\r\n")
            except BrokenPipeError, ConnectionResetError:
                pass

        def log_message(self, format: str, *args: object) -> None:
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), CameraHandler) as server:
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        try:
            assert_red_jpeg(camera.camera_frame(f"http://127.0.0.1:{server.server_port}/camera"))
        finally:
            server.shutdown()
            thread.join(timeout=2)
        assert not thread.is_alive()


@pytest.mark.parametrize("outcome", ["success", "timeout", "failure", "missing", "empty", "oversize"])
def test_snapshot_limits_cleanup_and_private_diagnostics(
    outcome: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    paths: list[Path] = []

    def run(command: list[str], **options: object) -> subprocess.CompletedProcess[bytes]:
        output = Path(command[-1])
        paths.append(output)
        assert options["timeout"] == camera.CAMERA_TIMEOUT
        assert all(options[stream] == subprocess.DEVNULL for stream in ("stdin", "stdout", "stderr"))
        assert command[command.index("-rw_timeout") + 1] == "5000000"
        if outcome == "missing":
            return subprocess.CompletedProcess(command, 0)
        output.write_bytes(b"jpeg" if outcome == "success" else b"")
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(command, camera.CAMERA_TIMEOUT, stderr=b"private native error")
        if outcome == "failure":
            raise subprocess.CalledProcessError(1, command, stderr=b"private native error")
        if outcome == "oversize":
            with output.open("wb") as file:
                file.seek(camera.CAMERA_MAX_BYTES)
                file.write(b"x")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(camera.subprocess, "run", run)
    result = camera.camera_frame("http://camera.invalid/?private-source")
    assert result == (b"jpeg" if outcome == "success" else None)
    assert paths and all(not output.parent.exists() for output in paths)
    assert "private-source" not in caplog.text
    assert "private native error" not in caplog.text


@pytest.mark.parametrize("results, expected_calls", [([b"jpeg"], 1), ([None, b"jpeg"], 2), ([None, None], 2)])
def test_camera_retries_once(results: list[bytes | None], expected_calls: int, monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot = Mock(side_effect=results)
    monkeypatch.setattr(camera, "camera_frame", snapshot)
    assert camera.camera("msu") == results[-1]
    assert snapshot.call_count == expected_calls
    snapshot.assert_called_with(camera.CAMERA_URL)
    snapshot.reset_mock()
    assert camera.camera("unknown") is None
    snapshot.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("content", [b"jpeg", None])
async def test_cache_keeps_success_and_failure_for_ten_seconds(content: bytes | None, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [0.0]
    monkeypatch.setattr(camera, "_camera_cache", TTLCache(maxsize=1, ttl=10, timer=lambda: clock[0]))
    run = AsyncMock(return_value=(content, False))
    worker = cast(TPExecutor, SimpleNamespace(run=run))
    assert await camera._camera_msu(worker) == content
    clock[0] = 9.99
    assert await camera._camera_msu(worker) == content
    assert run.await_count == 1
    clock[0] = 10.0
    assert await camera._camera_msu(worker) == content
    assert run.await_count == 2


@pytest.mark.asyncio
async def test_cached_photo_has_a_fresh_buffer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(camera, "_camera_cache", TTLCache(maxsize=1, ttl=10))
    worker = cast(TPExecutor, SimpleNamespace(run=AsyncMock(return_value=(b"jpeg", False))))
    first, second = await camera.camera_msu(worker), await camera.camera_msu(worker)
    assert first is not None and second is not None and first is not second
    assert first.read() == b"jpeg"
    assert second.read() == b"jpeg"


@pytest.mark.asyncio
async def test_photo_preserves_reply_target_topic_and_failure_text(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = make_bot()
    target = make_message(bot, message_id=8, is_topic_message=True, message_thread_id=9)
    message = make_message(bot, reply_to_message=target, is_topic_message=True, message_thread_id=9)
    worker = cast(TPExecutor, SimpleNamespace())
    monkeypatch.setattr(camera, "camera_msu", AsyncMock(side_effect=[io.BytesIO(b"jpeg"), None]))
    await camera.Camera.process(message, worker)
    sent = bot.session.methods[-1]
    assert isinstance(sent, SendPhoto)
    assert isinstance(sent.photo, BufferedInputFile)
    assert sent.photo.filename == "camera.jpg"
    assert sent.reply_parameters is not None and sent.reply_parameters.message_id == 8
    assert sent.message_thread_id == 9
    await camera.Camera.process(message, worker)
    failure = bot.session.methods[-1]
    assert isinstance(failure, SendMessage)
    assert failure.text == "😔 Камера недоступна"
    assert failure.reply_parameters is not None and failure.reply_parameters.message_id == message.message_id


@pytest.mark.asyncio
async def test_stop_removes_buttons_without_capturing(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = make_bot()
    message = make_message(bot)
    query = CallbackQuery.model_validate(
        {"id": "click", "from_user": message.from_user, "chat_instance": "chat", "message": message, "data": "camera:msu:stop"},
        context={"bot": bot},
    )
    snapshot = AsyncMock()
    monkeypatch.setattr(camera, "camera_msu", snapshot)
    await camera.Camera.process_cb(query, camera.CameraCallback(name="msu", action="stop"), cast(TPExecutor, SimpleNamespace()))
    acknowledgement, edit = bot.session.methods
    assert isinstance(acknowledgement, AnswerCallbackQuery)
    assert acknowledgement.text == "✅ Вид сохранён"
    assert isinstance(edit, EditMessageReplyMarkup)
    assert edit.reply_markup is None
    snapshot.assert_not_awaited()
