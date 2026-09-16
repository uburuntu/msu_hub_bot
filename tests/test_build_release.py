import importlib.util
import json
from pathlib import Path
from subprocess import CompletedProcess

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("build_release", ROOT / "tools/build_release.py")
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


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
