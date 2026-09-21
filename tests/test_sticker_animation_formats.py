"""Native APNG/WEBP timing, compositing and bounded-media regressions."""

import io
import errno
import os
import random
import shutil
import subprocess
import threading
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from msu_hub_bot.media import sticker_media as media


def animation(fmt, colors=((255, 0, 0, 128), (0, 255, 0, 128), (0, 0, 255, 128)), durations=(100, 400, 200), **kwargs):
    frames = [Image.new("RGBA", (32, 32), color) for color in colors]
    output = io.BytesIO()
    frames[0].save(output, format=fmt, save_all=True, append_images=frames[1:], duration=durations, loop=kwargs.pop("loop", 0), **kwargs)
    return output.getvalue()


def decode(tmp_path, prepared, side=1):
    output = tmp_path / "result.webm"
    output.write_bytes(prepared.payload)
    _, stream, duration = media._probe(output)
    assert max(stream["width"], stream["height"]) == 512
    assert 0 < duration <= 3 and len(prepared.payload) <= media.MAX_VIDEO_BYTES
    result = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-c:v",
            "libvpx-vp9",
            "-i",
            str(output),
            "-vf",
            f"format=rgba,scale={side}:{side}",
            "-pix_fmt",
            "rgba",
            "-f",
            "rawvideo",
            "pipe:1",
        ],
        check=True,
        capture_output=True,
        timeout=30,
    ).stdout
    frame_size = side * side * 4
    return duration, [result[index : index + frame_size] for index in range(0, len(result), frame_size)]


native = pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg and FFprobe required")


@native
@pytest.mark.parametrize("fmt", ["PNG", "WEBP"])
@pytest.mark.parametrize("loop", [0, 2])
def test_frame_timing_alpha_and_one_cycle_are_preserved(tmp_path, fmt, loop):
    prepared = media.prepare_media(animation(fmt, loop=loop), "static")
    assert prepared.kind == "video" and not prepared.trimmed
    duration, frames = decode(tmp_path, prepared)
    assert 0.69 <= duration <= 0.735
    colors = [max(range(3), key=lambda component: frame[component]) for frame in frames]
    assert abs(colors.count(0) - 3) <= 1
    assert abs(colors.count(1) - 12) <= 1
    assert abs(colors.count(2) - 6) <= 1
    assert all(120 <= frame[3] <= 136 for frame in frames)


@native
def test_stronger_encoding_fits_detailed_animation_without_losing_alpha_or_timing(tmp_path, monkeypatch):
    rng = random.Random(13)
    frames = []
    for _ in range(7):
        frame = Image.frombytes("RGB", (96, 96), rng.randbytes(96 * 96 * 3))
        frame.putalpha(128)
        frames.append(frame)
    source = io.BytesIO()
    frames[0].save(source, format="PNG", save_all=True, append_images=frames[1:], duration=100, loop=0)
    for frame in frames:
        frame.close()
    run = media._run
    encoded = []

    def record(command, **kwargs):
        result = run(command, **kwargs)
        if command[0] == "ffmpeg":
            size = Path(command[-1]).stat().st_size
            encoded.append((command[command.index("-crf") + 1], size))
            # Keep the fallback exercised on FFmpeg versions that compress the
            # fixture below Telegram's cap on the first pass already.
            if len(encoded) == 1:
                monkeypatch.setattr(media, "MAX_VIDEO_BYTES", min(media.MAX_VIDEO_BYTES, size - 1))
        return result

    monkeypatch.setattr(media, "_run", record)
    prepared = media.prepare_media(source.getvalue(), "static")
    duration, decoded = decode(tmp_path, prepared)
    assert [quality for quality, _ in encoded] == ["32", "42"]
    assert encoded[1][1] <= media.MAX_VIDEO_BYTES < encoded[0][1]
    assert 0.69 <= duration <= 0.735 and not prepared.trimmed
    assert all(120 <= frame[3] <= 136 for frame in decoded)
    info, stream, _ = media._probe(tmp_path / "result.webm")
    assert stream["avg_frame_rate"] == "30/1"
    assert all(stream["codec_type"] != "audio" for stream in info["streams"])


@native
@pytest.mark.parametrize("fmt", ["PNG", "WEBP"])
def test_only_first_seven_seconds_enter_long_animation(tmp_path, fmt):
    prepared = media.prepare_media(animation(fmt, durations=[3000, 4000, 2000]), "static")
    assert prepared.trimmed
    duration, frames = decode(tmp_path, prepared)
    assert 2.8 < duration <= 3
    colors = {max(range(3), key=lambda component: frame[component]) for frame in frames}
    assert colors == {0, 1}


@native
def test_apng_default_poster_is_not_an_animation_frame(tmp_path):
    data = animation("PNG", durations=[100, 200], default_image=True)
    duration, frames = decode(tmp_path, media.prepare_media(data, "static"))
    assert 0.29 <= duration <= 0.335
    assert {max(range(3), key=lambda component: frame[component]) for frame in frames} == {1, 2}


@native
def test_apng_partial_frames_respect_blending_and_previous_disposal(tmp_path):
    base = Image.new("RGBA", (32, 32), "red")
    overlay = Image.new("RGBA", (32, 32))
    ImageDraw.Draw(overlay).rectangle((0, 0, 15, 31), fill="green")
    last = Image.new("RGBA", (32, 32))
    ImageDraw.Draw(last).rectangle((0, 16, 31, 31), fill="blue")
    output = io.BytesIO()
    base.save(output, format="PNG", save_all=True, append_images=[overlay, last], duration=200, blend=[0, 1, 1], disposal=[0, 2, 0])
    _, frames = decode(tmp_path, media.prepare_media(output.getvalue(), "static"), side=2)
    # Middle frame overlays green on red; previous disposal restores red for last.
    middle, final = frames[7], frames[13]
    assert middle[1] > middle[0] and middle[4] > middle[5]
    assert final[0] > final[1] and final[10] > final[8]


@pytest.mark.parametrize("fmt", ["PNG", "WEBP"])
def test_nonpositive_frame_duration_is_rejected_before_encoding(monkeypatch, fmt):
    monkeypatch.setattr(media, "_encode_video", lambda *args, **kwargs: pytest.fail("invalid timeline reached encoder"))
    with pytest.raises(media.StickerMediaError, match="длительность кадра"):
        media.prepare_media(animation(fmt, durations=[0, 100, 200]), "static")


@pytest.mark.parametrize("limit", ["MAX_ANIMATION_FRAMES", "MAX_ANIMATION_PIXELS", "MAX_FRAME_BYTES"])
def test_animation_limits_apply_before_encoding(monkeypatch, limit):
    monkeypatch.setattr(media, limit, 1)
    monkeypatch.setattr(media, "_encode_video", lambda *args, **kwargs: pytest.fail("oversized animation reached encoder"))
    with pytest.raises(media.StickerMediaError):
        media.prepare_media(animation("PNG"), "static")


def test_animation_temporary_frames_are_removed_after_encoding_failure(monkeypatch):
    directories = []

    def fail(source, *args, **kwargs):
        directories.append(Path(source).parent)
        assert list(Path(source).parent.glob("frame-*.png"))
        raise media.StickerMediaError("synthetic encoding failure")

    monkeypatch.setattr(media, "_encode_video", fail)
    with pytest.raises(media.StickerMediaError):
        media.prepare_media(animation("WEBP"), "static")
    assert directories and all(not directory.exists() for directory in directories)


def test_static_preparation_releases_decoded_and_resized_image_cores(monkeypatch):
    payload = io.BytesIO()
    Image.new("RGBA", (32, 32), "red").save(payload, format="PNG")
    source = Image.open(io.BytesIO(payload.getvalue()))
    resized = []
    original_resize = media._resize

    def resize(image):
        result = original_resize(image)
        resized.append(result)
        return result

    monkeypatch.setattr(media.Image, "open", lambda file: source)
    monkeypatch.setattr(media, "_resize", resize)
    assert media.prepare_static(payload.getvalue()).kind == "static"
    for image in [source, *resized]:
        with pytest.raises(ValueError, match="closed"):
            image.getpixel((0, 0))


@native
def test_uploaded_playlist_cannot_open_a_referenced_local_media_file(tmp_path, monkeypatch):
    from msu_hub_bot.execution.process import run_process

    # A FIFO records whether FFprobe opens the playlist's synthetic media target.
    # The control without the format allowlist demonstrates that it would be read.
    target = tmp_path / "referenced.ts"
    os.mkfifo(target)
    sample = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=red:s=32x32:r=25:d=0.12",
            "-c:v",
            "mpeg2video",
            "-threads",
            "1",
            "-f",
            "mpegts",
            "pipe:1",
        ],
        check=True,
        capture_output=True,
        timeout=10,
    ).stdout
    playlist = tmp_path / "playlist.m3u8"
    playlist.write_text(f"#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:1\n#EXTINF:0.12,\n{target}\n#EXT-X-ENDLIST\n")

    def run_with_witness(function):
        stop, opened = threading.Event(), threading.Event()

        def supply_media():
            while not stop.is_set():
                try:
                    descriptor = os.open(target, os.O_WRONLY | os.O_NONBLOCK)
                except OSError as exc:
                    if exc.errno != errno.ENXIO:
                        raise
                    stop.wait(0.001)
                else:
                    opened.set()
                    try:
                        try:
                            os.write(descriptor, sample)
                        except BrokenPipeError:
                            pass
                    finally:
                        os.close(descriptor)
                    return

        witness = threading.Thread(target=supply_media)
        witness.start()
        try:
            function()
        finally:
            stop.set()
            witness.join(2)
        assert not witness.is_alive()
        return opened.is_set()

    def control():
        try:
            run_process(
                ["ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe", "-show_streams", "-of", "json", str(playlist)],
                timeout=2,
                max_output_bytes=1024 * 1024,
            )
        except subprocess.CalledProcessError:
            pass

    assert run_with_witness(control), "The playlist fixture did not attempt to read its referenced media"

    def bounded(command, *, timeout, max_output_bytes):
        return run_process(command, timeout=min(timeout, 2), max_output_bytes=max_output_bytes)

    monkeypatch.setattr(media, "run_process", bounded)

    def protected():
        with pytest.raises(media.StickerMediaError, match="прочитать"):
            media.prepare_video(playlist.read_bytes())

    assert not run_with_witness(protected), "An uploaded playlist opened another local file"
