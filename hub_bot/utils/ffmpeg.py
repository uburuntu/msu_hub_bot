import io
import logging
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

from common.utils import FakeBytesIO


FFMPEG_TIMEOUT = 120
logger = logging.getLogger(__name__)


def ffmpeg(file: io.BytesIO, parameters: list[str] | None = None, out_suffix: str | None = None) -> io.BytesIO | None:
    return ffmpeg2(file, parameters2=parameters, out_suffix=out_suffix)


def ffmpeg2(
    file: io.BytesIO, parameters1: list[str] | None = None, parameters2: list[str] | None = None, out_suffix: str | None = None
) -> io.BytesIO | None:
    """Convert in a disposable workspace, killing native work at its own deadline."""
    with TemporaryDirectory(prefix="hub-media-") as directory:
        source = Path(directory) / "source"
        output = Path(directory) / ("result" + (out_suffix or ""))
        # Close the writer before FFmpeg opens even a very small input.
        source.write_bytes(file.read())
        command = [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-y",
            *(parameters1 or []),
            "-i",
            str(source),
            *(parameters2 or []),
            str(output),
        ]
        try:
            subprocess.run(
                command,
                check=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=FFMPEG_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            # subprocess.run kills and reaps the child before returning the error.
            logger.warning("FFmpeg conversion exceeded its deadline")
            return None
        except (OSError, subprocess.CalledProcessError):
            # Native diagnostics can include input text/URLs; do not print them.
            logger.warning("FFmpeg conversion failed")
            return None
        result = FakeBytesIO(output.read_bytes())
        result.name = output.name
        return result


def to_ogg_opus(file: io.BytesIO) -> io.BytesIO | None:
    parameters = [
        "-f",
        "ogg",
        "-codec:a",
        "libopus",
        "-vn",
    ]
    return ffmpeg(file, out_suffix=".ogg", parameters=parameters)
