import importlib.util
from pathlib import Path
from subprocess import CompletedProcess

import pytest

SPEC = importlib.util.spec_from_file_location("deploy_client", Path(__file__).resolve().parents[1] / "tools/deployment/client.py")
client = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(client)


def test_public_deployment_output_excludes_ssh_diagnostics(capsys):
    result = CompletedProcess(
        args=[],
        returncode=1,
        stdout="host banner at 192.0.2.42\nImage received; checking configuration and connections\n",
        stderr="Permission denied for operator@192.0.2.42 using /private/example/key",
    )
    client.report_result(result)
    output = capsys.readouterr()
    assert "Image received" in output.out
    assert "Deployment failed" in output.err
    assert "192.0.2.42" not in output.out + output.err
    assert "/private/example/key" not in output.out + output.err


@pytest.mark.parametrize("enabled", [None, "", "false", "0", "invalid"])
def test_disabled_deployment_omits_all_telemetry_credentials(enabled):
    environment = {
        "HUB_BOT_TOKEN": "bot-canary",
        "LOGFIRE_TOKEN": "write-canary",
        "LOGFIRE_API_KEY": "management-canary",
        "LOGFIRE_READ_TOKEN": "read-canary",
        "OTEL_EXPORTER_OTLP_HEADERS": "headers-canary",
        "GITHUB_TOKEN": "github-canary",
        "HUB_CONFIG_JSON": "envelope-canary",
    }
    expected = {"HUB_BOT_TOKEN": "bot-canary"}
    if enabled is not None:
        environment["HUB_TELEMETRY_ENABLED"] = enabled
        if enabled:
            expected["HUB_TELEMETRY_ENABLED"] = enabled
    assert client.runtime_environment(environment) == expected


@pytest.mark.parametrize("enabled", ["true", "TRUE", "1", "yes"])
def test_enabled_deployment_accepts_only_exact_project_write_token(enabled):
    environment = {
        "HUB_TELEMETRY_ENABLED": enabled,
        "HUB_ENVIRONMENT": "production",
        "LOGFIRE_TOKEN": "write-canary",
        "LOGFIRE_API_KEY": "management-canary",
        "LOGFIRE_READ_TOKEN": "read-canary",
        "LOGFIRE_TOKEN_EXTRA": "other-canary",
        "OTEL_EXPORTER_OTLP_HEADERS": "headers-canary",
        "LOGFIRE_SEND_TO_LOGFIRE": "true",
    }
    assert client.runtime_environment(environment) == {
        "HUB_TELEMETRY_ENABLED": enabled,
        "HUB_ENVIRONMENT": "production",
        "LOGFIRE_TOKEN": "write-canary",
    }
