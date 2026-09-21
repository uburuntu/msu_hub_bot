"""Local media preparation; no Telegram calls or credentials."""

import gzip
import io
import json
import math
import subprocess
import time
from dataclasses import dataclass
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast

from PIL import Image, ImageOps

from msu_hub_bot.execution.process import ProcessOutputTooLarge, run_process
from msu_hub_bot.media.limits import LOCAL_MEDIA_FORMATS, MAX_DOWNLOAD_BYTES, MediaDimensionsError, validate_dimensions

MAX_INPUT_BYTES = MAX_DOWNLOAD_BYTES
MAX_VIDEO_BYTES = 256 * 1024
MAX_SOURCE_SECONDS = 7
MAX_ANIMATION_FRAMES = 300
MAX_ANIMATION_PIXELS = 64 * 1024 * 1024
MAX_FRAME_BYTES = 64 * 1024 * 1024
ENCODING_TIMEOUT = 60


class StickerMediaError(ValueError):
    pass


class StickerSizeError(StickerMediaError):
    """Prepared artwork still exceeds Telegram's output-size limit."""


@dataclass(frozen=True)
class PreparedMedia:
    kind: str
    payload: bytes
    trimmed: bool = False


def speed_factor(duration: float) -> float:
    if not math.isfinite(duration) or duration <= 0:
        raise StickerMediaError("Не удалось определить длительность анимации.")
    duration = min(duration, MAX_SOURCE_SECONDS)
    # Leave a frame-sized margin for container timestamp rounding.
    return duration / 2.95 if duration > 3 else 1.0


def _run(command: list[str], *, timeout: float = 60) -> bytes:
    try:
        return run_process(command, timeout=timeout, max_output_bytes=1024 * 1024 if command[0] == "ffprobe" else 0)
    except FileNotFoundError as exc:
        raise StickerMediaError("На сервере нужны FFmpeg и FFprobe.") from exc
    except subprocess.TimeoutExpired as exc:
        raise StickerMediaError("Обработка заняла слишком много времени. Стикер не добавлен.") from exc
    except (subprocess.CalledProcessError, ProcessOutputTooLarge) as exc:
        raise StickerMediaError("Не удалось прочитать или преобразовать анимацию.") from exc


def _probe(path: Path, *, timeout: float = 60) -> tuple[dict[str, Any], dict[str, Any], float]:
    info: dict[str, Any] = json.loads(
        _run(
            [
                "ffprobe",
                "-protocol_whitelist",
                "file,pipe",
                "-format_whitelist",
                LOCAL_MEDIA_FORMATS,
                "-v",
                "error",
                "-show_streams",
                "-show_format",
                "-of",
                "json",
                str(path),
            ],
            timeout=timeout,
        )
    )
    streams = info.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise StickerMediaError("В файле нет видео.")
    duration = video.get("duration") or info.get("format", {}).get("duration")
    try:
        duration = float(duration)
    except (TypeError, ValueError) as exc:
        raise StickerMediaError("Не удалось определить длительность анимации.") from exc
    return info, video, duration


def prepare_video(data: bytes) -> PreparedMedia:
    """Convert up to the first seven source seconds to a silent VP9 sticker."""
    if len(data) > MAX_INPUT_BYTES:
        raise StickerMediaError("Файл больше 20 МБ. Стикер не добавлен.")
    with TemporaryDirectory(prefix="hub-sticker-") as directory:
        source = Path(directory) / "source"
        output = Path(directory) / "sticker.webm"
        source.write_bytes(data)
        _, video, duration = _probe(source)
        return _encode_video(source, output, video, duration, trimmed=duration > MAX_SOURCE_SECONDS)


def _encode_video(
    source: Path,
    output: Path,
    video: dict[str, Any],
    duration: float,
    *,
    trimmed: bool,
    input_options: tuple[str, ...] = (),
) -> PreparedMedia:
    factor = speed_factor(duration)
    if video:
        try:
            validate_dimensions(int(video["width"]), int(video["height"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise StickerMediaError("Не удалось прочитать размеры видео или оно слишком большое. Уменьшите разрешение.") from exc
    # Input -t clips the original timeline, before setpts accelerates it.
    source_limit = ["-t", str(min(duration, MAX_SOURCE_SECONDS))] if trimmed or input_options else []
    # Preserve alpha when decoding existing VP9 video stickers.
    decoder = ["-c:v", "libvpx-vp9"] if video.get("codec_name") == "vp9" else []
    # Preserve display proportions before changing the pixel aspect ratio to 1.
    filters = (
        f"setpts=(PTS-STARTPTS)/{factor:.12f},"
        "scale=w='if(gte(dar,1),512,max(2,trunc(512*dar/2)*2))':"
        "h='if(gte(dar,1),max(2,trunc(512/dar/2)*2),512)',"
        "setsar=1,fps=30:round=down,format=yuva420p"
    )
    command = [
        "ffmpeg",
        "-nostdin",
        "-v",
        "error",
        "-y",
        "-protocol_whitelist",
        "file,pipe",
        "-format_whitelist",
        LOCAL_MEDIA_FORMATS + (",concat" if input_options else ""),
        "-filter_threads",
        "1",
        "-threads",
        "2",
        *source_limit,
        *input_options,
        *decoder,
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-an",
        "-sn",
        "-dn",
        "-vf",
        filters,
        "-c:v",
        "libvpx-vp9",
        "-b:v",
        "0",
        "-crf",
        "32",
        "-deadline",
        "good",
        "-cpu-used",
        "4",
        "-threads",
        "2",
        "-auto-alt-ref",
        "0",
        str(output),
    ]
    deadline = time.monotonic() + ENCODING_TIMEOUT

    def remaining() -> float:
        budget = deadline - time.monotonic()
        if budget <= 0:
            raise StickerMediaError("Обработка заняла слишком много времени. Стикер не добавлен.")
        return budget

    for quality in (32, 42):
        # Retry only a valid oversized encoding. Keep the original source, alpha,
        # dimensions and timeline; the second pass trades detail for smaller bytes.
        command[command.index("-crf") + 1] = str(quality)
        output.unlink(missing_ok=True)
        _run(command, timeout=remaining())
        info, result, length = _probe(output, timeout=remaining())
        width, height = result["width"], result["height"]
        fps_num, fps_den = result.get("avg_frame_rate", "0/1").split("/")
        fps = float(fps_num) / float(fps_den or 1)
        if (
            not 0 < length <= 3
            or max(width, height) != 512
            or min(width, height) <= 0
            or not 0 < fps <= 30
            or result.get("codec_name") != "vp9"
            or any(s.get("codec_type") == "audio" for s in info["streams"])
        ):
            raise StickerMediaError("Результат не соответствует ограничениям Telegram.")
        if output.stat().st_size <= MAX_VIDEO_BYTES:
            return PreparedMedia("video", output.read_bytes(), trimmed=trimmed)
    raise StickerSizeError("Даже после дополнительного сжатия стикер превышает 256 КБ. Попробуйте более простой или короткий фрагмент.")


def _check_dimensions(size: tuple[int, int]) -> None:
    try:
        validate_dimensions(*size)
    except MediaDimensionsError as exc:
        raise StickerMediaError("Картинка слишком большая по разрешению. Уменьшите её и повторите /sc.") from exc


def _resize(image: Image.Image) -> Image.Image:
    ratio = 512 / max(image.size)
    size = cast(tuple[int, int], tuple(max(1, round(value * ratio)) for value in image.size))
    return image.resize(size, Image.Resampling.LANCZOS)


def prepare_animation(source: Image.Image) -> PreparedMedia:
    """Decode one APNG/WEBP cycle, including disposal, before bounded encoding."""
    _check_dimensions(source.size)
    first = 1 if source.info.get("default_image", False) else 0
    count = getattr(source, "n_frames", 1)
    if not isinstance(count, int) or count <= first:
        raise StickerMediaError("В анимации нет кадров.")
    if count - first > MAX_ANIMATION_FRAMES:
        raise StickerMediaError("В анимации слишком много кадров. Пришлите короткий GIF или видео.")
    pixels = source.width * source.height
    elapsed = 0.0
    decoded = 0
    frame_bytes = 0
    manifest = ["ffconcat version 1.0"]
    with TemporaryDirectory(prefix="hub-sticker-frames-") as directory:
        root = Path(directory)
        for index in range(first, count):
            decoded += pixels
            if decoded > MAX_ANIMATION_PIXELS:
                raise StickerMediaError("Анимация слишком тяжёлая. Уменьшите разрешение или пришлите короткое видео.")
            source.seek(index)
            # Pillow applies APNG disposal/blending and WEBP compositing on load.
            with closing(source.convert("RGBA")) as rgba:
                value = source.info.get("duration")
                if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                    raise StickerMediaError("В анимации некорректная длительность кадра.")
                duration = float(value) / 1000
                frame = root / f"frame-{index}.png"
                with closing(_resize(rgba)) as resized:
                    resized.save(frame, format="PNG")
            frame_bytes += frame.stat().st_size
            if frame_bytes > MAX_FRAME_BYTES:
                raise StickerMediaError("Анимация слишком тяжёлая. Уменьшите разрешение или пришлите короткое видео.")
            kept = min(duration, MAX_SOURCE_SECONDS - elapsed)
            manifest.extend([f"file '{frame.name}'", "option framerate 1000", f"duration {kept:.9f}"])
            elapsed += duration
            if elapsed >= MAX_SOURCE_SECONDS:
                break
        if elapsed <= 0:
            raise StickerMediaError("В анимации нет кадров.")
        source.close()
        trimmed = elapsed > MAX_SOURCE_SECONDS or index < count - 1
        # Concat needs a following packet to retain the last frame's duration.
        manifest.extend([f"file '{frame.name}'", "option framerate 1000"])
        timeline = root / "timeline.ffconcat"
        timeline.write_text("\n".join(manifest) + "\n")
        # Every manifest path is generated above, never taken from the input.
        return _encode_video(
            timeline,
            root / "sticker.webm",
            {},
            min(elapsed, MAX_SOURCE_SECONDS),
            trimmed=trimmed,
            input_options=("-f", "concat", "-safe", "0"),
        )


def prepare_static(data: bytes) -> PreparedMedia:
    with closing(Image.open(io.BytesIO(data))) as source:
        _check_dimensions(source.size)
        if getattr(source, "is_animated", False):
            if source.format in {"PNG", "WEBP"}:
                return prepare_animation(source)
            return prepare_video(data)
        with (
            closing(ImageOps.exif_transpose(source)) as oriented,
            closing(oriented.convert("RGBA")) as rgba,
            closing(_resize(rgba)) as image,
        ):
            with io.BytesIO() as output:
                image.save(output, format="WEBP", lossless=True)
                if output.tell() > 512 * 1024:
                    raise StickerSizeError("После одной попытки сжатия картинка превышает 512 КБ. Стикер не добавлен.")
                return PreparedMedia("static", output.getvalue())


def prepare_tgs(data: bytes) -> bytes:
    # Only existing Telegram TGS stickers are accepted, not arbitrary Lottie files.
    if len(data) > 64 * 1024:
        raise StickerMediaError("TGS-стикер больше 64 КБ.")
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as source:
            unpacked = source.read(2 * 1024 * 1024 + 1)
        if len(unpacked) > 2 * 1024 * 1024:
            raise ValueError("oversized TGS")
        animation = json.loads(unpacked)
        first, last, fps = float(animation["ip"]), float(animation["op"]), float(animation["fr"])
        if not all(math.isfinite(value) for value in (first, last, fps)) or last <= first or not 0 < fps <= 60:
            raise ValueError("invalid TGS timeline")
        duration = (last - first) / fps
        speed_factor(duration)
        if duration > 3:
            # Existing valid Telegram TGS stickers already have a <=3s timeline.
            # Retiming arbitrary Lottie requires handling precompositions and time remapping.
            raise StickerMediaError("Некорректный TGS-стикер: длительность больше 3 секунд. Пришлите GIF или видео.")
    except StickerMediaError:
        raise
    except (OSError, EOFError, ValueError, KeyError, TypeError, ZeroDivisionError, RecursionError) as exc:
        raise StickerMediaError("Не удалось прочитать TGS-стикер.") from exc
    return data


def prepare_custom_emoji(data: bytes, kind: str) -> PreparedMedia:
    """Regular-colour custom emoji become regular stickers on a 512px canvas."""
    if kind != "animated":
        return prepare_media(data, kind)
    prepare_tgs(data)
    try:
        animation = json.loads(gzip.decompress(data))
        width, height = animation["w"], animation["h"]
        if isinstance(width, bool) or isinstance(height, bool) or not isinstance(width, int) or not isinstance(height, int):
            raise ValueError("invalid dimensions")
        _check_dimensions((width, height))
        layers = animation["layers"]
        assets = animation.get("assets", [])
        if not isinstance(layers, list) or not isinstance(assets, list) or not all(isinstance(asset, dict) for asset in assets):
            raise ValueError("invalid animation layers")
        if width == height == 512:
            return PreparedMedia("animated", data)
        identity = "hub_sticker_canvas"
        while any(asset.get("id") == identity for asset in assets):
            identity += "_"
        factor = 512 / max(width, height)
        animation["assets"] = [*assets, {"id": identity, "w": width, "h": height, "layers": layers}]
        animation["w"] = animation["h"] = 512
        animation["layers"] = [
            {
                "ty": 0,
                "ind": 1,
                "refId": identity,
                "w": width,
                "h": height,
                "ip": animation["ip"],
                "op": animation["op"],
                "st": 0,
                "ks": {
                    "o": {"a": 0, "k": 100},
                    "r": {"a": 0, "k": 0},
                    "a": {"a": 0, "k": [0, 0, 0]},
                    "p": {"a": 0, "k": [(512 - width * factor) / 2, (512 - height * factor) / 2, 0]},
                    "s": {"a": 0, "k": [factor * 100, factor * 100, 100]},
                },
            }
        ]
        result = gzip.compress(json.dumps(animation, ensure_ascii=False, separators=(",", ":")).encode(), mtime=0)
        return PreparedMedia("animated", prepare_tgs(result))
    except (OSError, ValueError, KeyError, TypeError, RecursionError) as exc:
        if isinstance(exc, StickerMediaError):
            raise
        raise StickerMediaError("Не удалось подготовить анимированный эмодзи. Пришлите его как GIF или видео.") from exc


def prepare_media(data: bytes, kind: str) -> PreparedMedia:
    if len(data) > MAX_INPUT_BYTES:
        raise StickerMediaError("Файл больше 20 МБ. Стикер не добавлен.")
    if kind == "animated":
        return PreparedMedia("animated", prepare_tgs(data))
    if kind == "video":
        return prepare_video(data)
    try:
        return prepare_static(data)
    except (OSError, ValueError, EOFError, SyntaxError, Image.DecompressionBombError) as exc:
        if isinstance(exc, StickerMediaError):
            raise
        raise StickerMediaError("Не удалось прочитать картинку.") from exc
