import importlib.util
import json
from pathlib import Path
from subprocess import CompletedProcess

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("build_release", ROOT / "tools/deployment/build_release.py")
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)
REVISION = "a" * 40


def configure(monkeypatch, tmp_path, *, env=(), labels=None):
    calls = []
    labels = {"org.opencontainers.image.revision": REVISION} if labels is None else labels

    def run(*arguments, **kwargs):
        calls.append(arguments)
        if arguments[:3] == ("docker", "image", "inspect"):
            return CompletedProcess(arguments, 0, stdout=json.dumps([{"Config": {"Env": list(env), "Labels": labels}}]))
        return CompletedProcess(arguments, 0)

    monkeypatch.chdir(ROOT)
    monkeypatch.setenv("GITHUB_SHA", REVISION)
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "output"))
    for name in ("ACTIONS_RUNTIME_TOKEN", "ACTIONS_RESULTS_URL", "ACTIONS_CACHE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(builder, "run", run)
    monkeypatch.setattr(builder, "containerd_store", lambda: True)
    return calls


def test_build_smoke_tests_and_reports_the_revision_tag(monkeypatch, tmp_path):
    calls = configure(monkeypatch, tmp_path)
    builder.main()
    build = calls[0]
    assert build[:3] == ("docker", "buildx", "build")
    assert "--load" in build and "--provenance=false" in build and "--cache-to" not in build
    assert build[build.index("--tag") + 1] == "msu-hub-bot:" + REVISION
    assert calls[1][:2] == ("docker", "run") and "--network" in calls[1]
    assert (tmp_path / "output").read_text() == "image=msu-hub-bot:" + REVISION + "\n"


def test_actions_cache_keeps_unchanged_layers_between_builds():
    assert builder.cache_arguments({}) == []
    assert builder.cache_arguments({"ACTIONS_RUNTIME_TOKEN": "token"}) == []
    arguments = builder.cache_arguments({"ACTIONS_RUNTIME_TOKEN": "token", "ACTIONS_RESULTS_URL": "https://results.invalid/"})
    assert arguments == ["--cache-from", "type=gha,scope=release", "--cache-to", "type=gha,scope=release,mode=max"]
    runtime = {"ACTIONS_RUNTIME_TOKEN": "token", "ACTIONS_RESULTS_URL": "https://results.invalid/"}
    assert builder.cache_arguments(runtime, supported=False) == []


@pytest.mark.parametrize("variable", ["HUB_BOT_TOKEN", "LOGFIRE_TOKEN", "LOGFIRE_API_KEY", "OTEL_EXPORTER_OTLP_HEADERS"])
def test_build_rejects_baked_runtime_credentials(monkeypatch, tmp_path, variable):
    configure(monkeypatch, tmp_path, env=[variable + "=private-canary"])
    with pytest.raises(SystemExit, match="Image contains runtime configuration") as caught:
        builder.main()
    assert "private-canary" not in str(caught.value)
    assert not (tmp_path / "output").exists()


def test_build_rejects_a_foreign_revision_label(monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path, labels={"org.opencontainers.image.revision": "b" * 40})
    with pytest.raises(SystemExit, match="revision mismatch"):
        builder.main()
