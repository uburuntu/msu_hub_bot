"""Offline foreground masks in a killable CPU worker with bounded images."""

import io
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from msu_hub_bot.execution.process import ProcessOutputTooLarge, run_process
from msu_hub_bot.media.limits import MAX_DOWNLOAD_BYTES

BACKGROUND_TIMEOUT = 45
MAX_BACKGROUND_BYTES = 20 * 1024 * 1024
MAX_OUTPUT_SIDE = 2048


class BackgroundRemovalError(ValueError):
    """The local model could not produce a bounded foreground image."""


def remove_background(file: io.BytesIO) -> bytes:
    with file.getbuffer() as view:
        if view.nbytes > MAX_DOWNLOAD_BYTES:
            raise BackgroundRemovalError("Изображение слишком большое.")
    with tempfile.TemporaryDirectory(prefix="bot-background-") as directory:
        source = Path(directory) / "input"
        source.write_bytes(file.getvalue())
        try:
            return run_process(
                [sys.executable, "-m", "msu_hub_bot.media.background", str(source)],
                timeout=BACKGROUND_TIMEOUT,
                max_output_bytes=MAX_BACKGROUND_BYTES,
            )
        except subprocess.SubprocessError, ProcessOutputTooLarge:
            raise BackgroundRemovalError("Не удалось убрать фон. Попробуй другое фото.") from None


def _limit_worker() -> None:
    # These apply only to the isolated worker, before the numerical libraries load.
    os.environ["OMP_NUM_THREADS"] = "1"
    os.environ["OPENBLAS_NUM_THREADS"] = "1"
    os.environ["MKL_NUM_THREADS"] = "1"
    import resource

    resource.setrlimit(resource.RLIMIT_CPU, (40, 40))
    if sys.platform == "linux":
        resource.setrlimit(resource.RLIMIT_AS, (1024 * 1024 * 1024, 1024 * 1024 * 1024))


def _render(source: Path) -> bytes:
    import numpy as np
    import onnxruntime as ort
    from PIL import Image, ImageChops, ImageOps

    from msu_hub_bot.media.background_model import verified_model
    from msu_hub_bot.media.limits import validate_dimensions

    with Image.open(source) as raw:
        validate_dimensions(*raw.size)
        with ImageOps.exif_transpose(raw) as oriented:
            oriented.thumbnail((MAX_OUTPUT_SIDE, MAX_OUTPUT_SIDE), Image.Resampling.LANCZOS)
            image = oriented.convert("RGBA")
    with image:
        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        options.enable_cpu_mem_arena = False
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        options.log_severity_level = 4
        session = ort.InferenceSession(verified_model(), sess_options=options, providers=["CPUExecutionProvider"])
        with image.convert("RGB") as rgb, rgb.resize((320, 320), Image.Resampling.LANCZOS) as resized:
            pixels = np.asarray(resized, dtype=np.float32)
        pixels /= max(float(pixels.max()), 1e-6)
        pixels -= np.array([0.485, 0.456, 0.406], dtype=np.float32)
        pixels /= np.array([0.229, 0.224, 0.225], dtype=np.float32)
        tensor = np.expand_dims(pixels.transpose(2, 0, 1), 0)
        output = np.asarray(session.run(None, {session.get_inputs()[0].name: tensor})[0])
        if output.shape != (1, 1, 320, 320) or not np.isfinite(output).all():
            raise BackgroundRemovalError("Invalid foreground mask")
        mask = output[0, 0]
        minimum, maximum = float(mask.min()), float(mask.max())
        if maximum - minimum <= 1e-6:
            raise BackgroundRemovalError("Empty foreground mask")
        normalized = ((mask - minimum) * (255 / (maximum - minimum))).clip(0, 255).astype(np.uint8)
        with Image.fromarray(normalized) as small, small.resize(image.size, Image.Resampling.LANCZOS) as alpha:
            with image.getchannel("A") as original, ImageChops.multiply(original, alpha) as combined:
                image.putalpha(combined)
        with io.BytesIO() as result:
            image.save(result, format="PNG")
            if result.tell() > MAX_BACKGROUND_BYTES:
                raise BackgroundRemovalError("Foreground output is too large")
            return result.getvalue()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(2)
    _limit_worker()
    sys.stdout.buffer.write(_render(Path(sys.argv[1])))
