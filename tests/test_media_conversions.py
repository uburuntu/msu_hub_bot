import io
import json
import os
import shutil
import signal
import subprocess
import sys
import wave
from pathlib import Path

import pytest

from msu_hub_bot.media import ffmpeg as media
from msu_hub_bot.execution import process as native
from msu_hub_bot.media.limits import MediaDimensionsError


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is not installed")
@pytest.mark.parametrize("convert", [media.ffmpeg, media.ffmpeg2])
def test_small_input_is_readable_by_native_ffmpeg(convert):
    source = io.BytesIO()
    with wave.open(source, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\0\0" * 400)
    assert source.tell() < 1024
    source.seek(0)
    result = convert(source, out_suffix=".wav")
    assert result is not None and result.name == "result.wav"
    with wave.open(result) as audio:
        assert audio.getnframes() == 400
        assert audio.getframerate() == 8000
    result.close()
    assert result.closed


@pytest.mark.parametrize("failure", ["error", "missing", "timeout", "missing-output"])
def test_failure_removes_input_and_output_without_logging_native_details(monkeypatch, caplog, failure):
    paths = []

    def failed(command, **kwargs):
        source = Path(command[command.index("-i") + 1])
        output = Path(command[-1])
        paths.extend([source, output])
        assert source.read_bytes() == b"synthetic input"
        assert kwargs["timeout"] == media.FFMPEG_TIMEOUT
        if failure == "missing-output":
            return b""
        output.write_bytes(b"partial conversion")
        if failure == "missing":
            raise FileNotFoundError
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, kwargs["timeout"], stderr=b"private-native-details")
        raise subprocess.CalledProcessError(1, command, stderr=b"private-native-details")

    monkeypatch.setattr(media, "run_process", failed)
    monkeypatch.setattr(media, "_validate_source", lambda *args, **kwargs: None)
    assert media.ffmpeg(io.BytesIO(b"synthetic input"), out_suffix=".mp4") is None
    assert not any(path.exists() or path.parent.exists() for path in paths)
    assert "private-native-details" not in caplog.text


def test_native_deadline_kills_and_reaps_child(monkeypatch):
    popen = subprocess.Popen
    children = []
    source_paths = []

    def slow_native(command, **kwargs):
        source_paths.append(Path(command[command.index("-i") + 1]))
        child = popen([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(media, "FFMPEG_TIMEOUT", 0.5)
    monkeypatch.setattr(native.subprocess, "Popen", slow_native)
    monkeypatch.setattr(media, "_validate_source", lambda *args, **kwargs: None)
    assert media.ffmpeg(io.BytesIO(b"input"), out_suffix=".wav") is None
    assert len(children) == 1
    child = children[0]
    assert child.returncode == -signal.SIGKILL
    with pytest.raises(ProcessLookupError):
        os.kill(child.pid, 0)
    with pytest.raises(ChildProcessError):
        os.waitpid(child.pid, os.WNOHANG)
    assert not source_paths[0].parent.exists()


def test_oversized_output_is_rejected_without_loading_it(monkeypatch):
    paths = []

    def convert(command, **kwargs):
        output = Path(command[-1])
        paths.append(output)
        with output.open("wb") as stream:
            stream.truncate(media.MAX_OUTPUT_BYTES + 1)

    monkeypatch.setattr(media, "run_process", convert)
    monkeypatch.setattr(media, "_validate_source", lambda *args, **kwargs: None)
    assert media.ffmpeg(io.BytesIO(b"input"), out_suffix=".wav") is None
    assert not paths[0].parent.exists()


@pytest.mark.parametrize(
    "changes",
    [
        {"width": 8193},
        {"width": 5000, "height": 4000},
        {"duration": "0"},
        {"avg_frame_rate": "0/0", "r_frame_rate": "0/0"},
        {"nb_frames": "10000"},
        {"duration": "3600"},
    ],
)
def test_reverse_rejects_unbounded_or_oversized_decoded_inputs(monkeypatch, changes):
    stream = {"codec_type": "video", "width": 1280, "height": 720, "duration": "4", "avg_frame_rate": "30/1", "nb_frames": "120"}
    stream.update(changes)
    monkeypatch.setattr(media, "run_process", lambda *args, **kwargs: json.dumps({"streams": [stream]}).encode())
    with pytest.raises((MediaDimensionsError, media.ReverseMediaError)):
        media._validate_source(Path("synthetic.mp4"), reverse=True)


def test_reverse_budgets_video_and_audio_together(monkeypatch):
    video = {"codec_type": "video", "width": 1280, "height": 720, "duration": "4", "avg_frame_rate": "30/1", "nb_frames": "120"}
    audio = {"codec_type": "audio", "duration": "4", "channels": 2, "sample_rate": "48000"}
    payload = {"streams": [video, audio]}
    monkeypatch.setattr(media, "run_process", lambda *args, **kwargs: json.dumps(payload).encode())
    media._validate_source(Path("synthetic.mp4"), reverse=True)
    audio["duration"] = "3600"
    with pytest.raises(media.ReverseSizeError):
        media._validate_source(Path("synthetic.mp4"), reverse=True)


@pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="FFmpeg and FFprobe are not installed")
def test_native_audio_reverse_preserves_every_sample():
    source = io.BytesIO()
    samples = [sample.to_bytes(2, "little", signed=True) for sample in range(400)]
    with wave.open(source, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"".join(samples))
    source.seek(0)
    result = media.ffmpeg(source, parameters=["-af", "areverse"], out_suffix=".wav", reverse=True)
    assert result is not None
    with wave.open(result) as audio:
        assert audio.getnframes() == 400
        assert audio.readframes(400) == b"".join(reversed(samples))


def test_caller_budget_includes_probe_and_conversion(monkeypatch):
    now = [0.0]
    budgets = []
    monkeypatch.setattr(media, "time", type("Clock", (), {"monotonic": staticmethod(lambda: now[0])}))

    def probe(*args, timeout, **kwargs):
        budgets.append(timeout)
        now[0] += 2

    def convert(command, *, timeout):
        budgets.append(timeout)
        Path(command[-1]).write_bytes(b"converted")

    monkeypatch.setattr(media, "_validate_source", probe)
    monkeypatch.setattr(media, "run_process", convert)
    result = media.ffmpeg(io.BytesIO(b"input"), out_suffix=".wav", timeout=5)
    assert result is not None and budgets == [5, 3]


def test_expired_probe_budget_never_starts_conversion(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(media, "time", type("Clock", (), {"monotonic": staticmethod(lambda: now[0])}))

    def probe(*args, **kwargs):
        now[0] = 6

    def convert(*args, **kwargs):
        pytest.fail("conversion started after the caller deadline")

    monkeypatch.setattr(media, "_validate_source", probe)
    monkeypatch.setattr(media, "run_process", convert)
    assert media.ffmpeg(io.BytesIO(b"input"), out_suffix=".wav", timeout=5) is None


@pytest.mark.skipif(shutil.which("ffprobe") is None, reason="FFprobe is not installed")
def test_uploaded_playlist_cannot_open_a_local_fifo(tmp_path):
    target = tmp_path / "victim"
    os.mkfifo(target)
    source = tmp_path / "source"
    source.write_text("ffconcat version 1.0\nfile 'victim'\n")
    # Opening this FIFO would block until the deadline; rejection must happen first.
    with pytest.raises(subprocess.CalledProcessError):
        media._validate_source(source, reverse=False, timeout=2)
