import hashlib
import importlib.util
import io
import wave
from pathlib import Path

SPEC = importlib.util.spec_from_file_location("image_smoke", Path(__file__).resolve().parents[1] / "tools/deployment/image_smoke.py")
smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)


def test_fingerprint_input_matches_the_native_compatibility_fixture():
    audio = smoke.synthetic_audio()
    assert hashlib.sha256(audio).hexdigest() == "eb894cece71a84ee8180890a5b053abc99fa58d86f0e64ffbdf0f3aae850cc8d"
    with wave.open(io.BytesIO(audio), "rb") as source:
        assert source.getnchannels() == 1
        assert source.getsampwidth() == 2
        assert source.getframerate() == 8000
        assert source.getnframes() == 80000
