"""Fetch the fixed, licensed foreground model explicitly during image builds."""

import hashlib
import sys
import urllib.request
from pathlib import Path

MODEL_URL = "https://github.com/danielgatis/rembg/releases/download/v0.0.0/u2netp.onnx"
MODEL_SHA256 = "309c8469258dda742793dce0ebea8e6dd393174f89934733ecc8b14c76f4ddd8"
MODEL_BYTES = 4_574_861
MODEL_PATH = Path(__file__).with_name("_models") / "u2netp.onnx"


def verified_model(path: Path = MODEL_PATH) -> bytes:
    with path.open("rb") as source:
        data = source.read(MODEL_BYTES + 1)
    if len(data) != MODEL_BYTES or hashlib.sha256(data).hexdigest() != MODEL_SHA256:
        raise ValueError("Background model checksum mismatch")
    return data


def fetch_model() -> None:
    """Explicit maintenance/build operation; inference never calls this."""
    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = MODEL_PATH.with_suffix(".download")
    try:
        with urllib.request.urlopen(MODEL_URL, timeout=60) as response:
            data = response.read(MODEL_BYTES + 1)
        temporary.write_bytes(data)
        verified_model(temporary)
        temporary.replace(MODEL_PATH)
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    if sys.argv[1:] != ["fetch"]:
        raise SystemExit("Usage: python -m msu_hub_bot.media.background_model fetch")
    fetch_model()
