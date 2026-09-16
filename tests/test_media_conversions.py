import io
import os
import shutil
import subprocess
import sys
import wave
from pathlib import Path

import pytest

from hub_bot.utils import ffmpeg as media


@pytest.mark.skipif(shutil.which('ffmpeg') is None, reason='FFmpeg is not installed')
@pytest.mark.parametrize('convert', [media.ffmpeg, media.ffmpeg2])
def test_small_input_is_readable_by_native_ffmpeg(convert):
    source = io.BytesIO()
    with wave.open(source, 'wb') as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b'\0\0' * 400)
    assert source.tell() < 1024
    source.seek(0)
    result = convert(source, out_suffix='.wav')
    assert result is not None and result.name == 'result.wav'
    with wave.open(result) as audio:
        assert audio.getnframes() == 400
        assert audio.getframerate() == 8000


@pytest.mark.parametrize('failure', ['error', 'missing', 'timeout'])
def test_failure_removes_input_and_output_without_logging_native_details(monkeypatch, caplog, failure):
    paths = []

    def failed(command, **kwargs):
        source = Path(command[command.index('-i') + 1])
        output = Path(command[-1])
        paths.extend([source, output])
        assert source.read_bytes() == b'synthetic input'
        output.write_bytes(b'partial conversion')
        assert kwargs['timeout'] == media.FFMPEG_TIMEOUT
        assert kwargs['stderr'] == subprocess.DEVNULL
        if failure == 'missing':
            raise FileNotFoundError
        if failure == 'timeout':
            raise subprocess.TimeoutExpired(command, kwargs['timeout'], stderr=b'private-native-details')
        raise subprocess.CalledProcessError(1, command, stderr=b'private-native-details')

    monkeypatch.setattr(media.subprocess, 'run', failed)
    assert media.ffmpeg(io.BytesIO(b'synthetic input'), out_suffix='.mp4') is None
    assert not any(path.exists() or path.parent.exists() for path in paths)
    assert 'private-native-details' not in caplog.text


def test_native_deadline_kills_and_reaps_child(monkeypatch, tmp_path):
    run = subprocess.run
    pid_file = tmp_path / 'child.pid'
    source_paths = []

    def slow_native(command, **kwargs):
        source_paths.append(Path(command[command.index('-i') + 1]))
        return run(
            [sys.executable, '-c', 'import os, pathlib, sys, time; pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(30)', str(pid_file)],
            **kwargs,
        )

    monkeypatch.setattr(media, 'FFMPEG_TIMEOUT', 0.5)
    monkeypatch.setattr(media.subprocess, 'run', slow_native)
    assert media.ffmpeg(io.BytesIO(b'input'), out_suffix='.wav') is None
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)
    assert not source_paths[0].parent.exists()
