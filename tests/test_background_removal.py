"""Foreground processing owns its subprocess and preserves transparent edges."""

import io
import subprocess
from pathlib import Path
from unittest.mock import Mock

import numpy as np
import pytest
from PIL import Image

from msu_hub_bot.media import background, background_model
from msu_hub_bot.media.limits import MediaDimensionsError


@pytest.mark.parametrize("failure", [subprocess.TimeoutExpired("worker", 1), subprocess.CalledProcessError(1, "worker")])
def test_failed_worker_removes_input_and_does_not_expose_native_diagnostics(monkeypatch, failure):
    paths = []

    def run(command, *, timeout, max_output_bytes):
        paths.append(Path(command[-1]))
        assert paths[-1].read_bytes() == b"synthetic"
        assert timeout == 45 and max_output_bytes == 20 * 1024 * 1024
        raise failure

    monkeypatch.setattr(background, "run_process", run)
    with pytest.raises(background.BackgroundRemovalError, match="Не удалось убрать фон"):
        background.remove_background(io.BytesIO(b"synthetic"))
    assert paths and all(not path.exists() for path in paths)


def test_oversized_input_does_not_launch_worker(monkeypatch):
    monkeypatch.setattr(background, "MAX_DOWNLOAD_BYTES", 3)
    worker = Mock()
    monkeypatch.setattr(background, "run_process", worker)
    with pytest.raises(background.BackgroundRemovalError):
        background.remove_background(io.BytesIO(b"1234"))
    worker.assert_not_called()


def test_changed_model_fails_integrity_check(tmp_path):
    model = tmp_path / "model.onnx"
    model.write_bytes(b"not a trusted model")
    with pytest.raises(ValueError, match="checksum"):
        background_model.verified_model(model)


def _fake_model(monkeypatch, mask):
    import onnxruntime

    def create(model, *, sess_options, providers):
        assert model == b"synthetic model" and providers == ["CPUExecutionProvider"]
        assert sess_options.intra_op_num_threads == sess_options.inter_op_num_threads == 1

        def run(names, inputs):
            values = inputs["input"]
            assert values.shape == (1, 3, 320, 320) and values.dtype == np.float32
            assert np.isfinite(values).all()
            return [mask]

        return type(
            "Session", (), {"run": staticmethod(run), "get_inputs": staticmethod(lambda: [type("Input", (), {"name": "input"})()])}
        )()

    monkeypatch.setattr(onnxruntime, "InferenceSession", create)
    monkeypatch.setattr(background_model, "verified_model", lambda: b"synthetic model")


def test_mask_preserves_existing_transparency_and_uses_soft_edges(tmp_path, monkeypatch):
    mask = np.tile(np.linspace(0, 1, 320, dtype=np.float32), (320, 1))[None, None]
    _fake_model(monkeypatch, mask)
    source = tmp_path / "input.png"
    with Image.new("RGBA", (320, 320), (30, 80, 150, 128)) as image:
        image.save(source)
    with Image.open(io.BytesIO(background._render(source))) as result:
        assert result.mode == "RGBA" and result.size == (320, 320)
        assert result.getpixel((0, 100))[3] == 0
        assert 62 <= result.getpixel((160, 100))[3] <= 65
        assert result.getpixel((319, 100))[3] == 128
        assert result.getpixel((319, 100))[:3] == (30, 80, 150)


def test_black_input_has_finite_normalization_and_output_is_downscaled(tmp_path, monkeypatch):
    mask = np.tile(np.linspace(0, 1, 320, dtype=np.float32), (320, 1))[None, None]
    _fake_model(monkeypatch, mask)
    source = tmp_path / "input.png"
    with Image.new("RGB", (4000, 1000), "black") as image:
        image.save(source)
    with Image.open(io.BytesIO(background._render(source))) as result:
        assert result.size == (2048, 512)


@pytest.mark.parametrize("mask", [np.ones((1, 1, 320, 320)), np.zeros((1, 1, 2, 2)), np.full((1, 1, 320, 320), np.nan)])
def test_invalid_or_empty_mask_is_not_returned_as_a_successful_cutout(tmp_path, monkeypatch, mask):
    _fake_model(monkeypatch, mask)
    source = tmp_path / "input.png"
    with Image.new("RGB", (4, 4)) as image:
        image.save(source)
    with pytest.raises(background.BackgroundRemovalError):
        background._render(source)


def test_oversized_dimensions_rejected_before_inference(tmp_path, monkeypatch):
    source = tmp_path / "wide.png"
    with Image.new("RGB", (9000, 1)) as image:
        image.save(source)
    model = Mock()
    monkeypatch.setattr(background_model, "verified_model", model)
    with pytest.raises(MediaDimensionsError):
        background._render(source)
    model.assert_not_called()
