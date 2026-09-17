import hashlib
import importlib.util
import io
import json
import tarfile
from pathlib import Path
from subprocess import CompletedProcess

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("build_release", ROOT / "tools/deployment/build_release.py")
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


def image_archive(*, config=None, manifests=None, config_type=tarfile.REGTYPE):
    config = config or {
        "os": "linux",
        "architecture": "amd64",
        "config": {
            "User": "10001:10001",
            "Entrypoint": ["/opt/msu_hub_bot/.venv/bin/msu-hub-bot"],
            "Labels": {"org.opencontainers.image.revision": "a" * 40},
            "Env": ["PATH=/opt/msu_hub_bot/.venv/bin"],
        },
    }
    raw = json.dumps(config).encode()
    config_id = hashlib.sha256(raw).hexdigest()
    config_path = "blobs/sha256/" + config_id
    if manifests is None:
        manifests = [{"Config": config_path, "RepoTags": ["msu-hub-bot:" + "a" * 40], "Layers": []}]
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for name, content, kind in (
            ("manifest.json", json.dumps(manifests).encode(), tarfile.REGTYPE),
            (config_path, raw, config_type),
        ):
            member = tarfile.TarInfo(name)
            member.type = kind
            member.size = len(content) if kind == tarfile.REGTYPE else 0
            archive.addfile(member, io.BytesIO(content) if member.size else None)
    return output.getvalue(), "sha256:" + config_id


def configure_export(monkeypatch, tmp_path, exported):
    def run(*arguments, **kwargs):
        if arguments[:3] == ("docker", "image", "inspect"):
            metadata = {"Id": "sha256:" + "f" * 64, "Config": {"Env": []}}
            return CompletedProcess(arguments, 0, stdout=json.dumps([metadata]))
        return CompletedProcess(arguments, 0)

    class Export:
        stdout = io.BytesIO(exported)

        def wait(self):
            return 0

    monkeypatch.chdir(ROOT)
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "output"))
    monkeypatch.setattr(builder, "run", run)
    monkeypatch.setattr(builder.subprocess, "Popen", lambda *args, **kwargs: Export())
    monkeypatch.setattr(builder.os, "umask", lambda value: 0o077)


def test_export_uses_configuration_digest_when_inspect_returns_oci_index(monkeypatch, tmp_path):
    exported, config_id = image_archive()
    configure_export(monkeypatch, tmp_path, exported)
    builder.main()
    archive = Path((tmp_path / "output").read_text().strip().removeprefix("archive="))
    metadata = json.loads(archive.with_name("metadata.json").read_text())
    assert metadata["image"] == config_id
    assert metadata["archive_sha256"] == hashlib.sha256(archive.read_bytes()).hexdigest()
    assert metadata["archive_size"] == archive.stat().st_size


def test_configuration_digest_does_not_bypass_archive_contract(monkeypatch, tmp_path):
    exported, _ = image_archive(config={"os": "linux", "architecture": "arm64", "config": {}})
    configure_export(monkeypatch, tmp_path, exported)
    with pytest.raises(Exception, match="Unsupported image platform"):
        builder.main()
    assert not (tmp_path / "output").exists()
    assert not list(tmp_path.glob("*/metadata.json"))


@pytest.mark.parametrize(
    "options",
    [{"manifests": []}, {"manifests": [{}, {}]}, {"config_type": tarfile.SYMTYPE}, {"config": {"oversized": "a" * 1024**2}}],
)
def test_configuration_digest_rejects_invalid_metadata(tmp_path, options):
    import gzip

    exported, _ = image_archive(**options)
    archive = tmp_path / "image.tar.gz"
    archive.write_bytes(gzip.compress(exported))
    with pytest.raises(ValueError, match="Archive must contain exactly one image|Invalid image archive metadata"):
        builder.archive_image_id(archive)


@pytest.mark.parametrize("variable", ["HUB_BOT_TOKEN", "LOGFIRE_TOKEN", "LOGFIRE_API_KEY", "OTEL_EXPORTER_OTLP_HEADERS"])
def test_build_rejects_baked_runtime_credentials_before_export(monkeypatch, tmp_path, variable):
    calls = []

    def run(*arguments, **kwargs):
        calls.append(arguments)
        if arguments[:3] == ("docker", "image", "inspect"):
            return CompletedProcess(arguments, 0, stdout=json.dumps([{"Config": {"Env": [variable + "=private-canary"]}}]))
        return CompletedProcess(arguments, 0)

    monkeypatch.chdir(ROOT)
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setattr(builder, "run", run)
    with pytest.raises(SystemExit, match="Image contains runtime configuration") as caught:
        builder.main()
    assert "private-canary" not in str(caught.value)
    assert not list(tmp_path.iterdir())
    assert not any(arguments[:3] == ("docker", "image", "save") for arguments in calls)
