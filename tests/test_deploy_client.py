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


@pytest.mark.parametrize("enabled", ["true", "false"])
def test_jev_uses_the_runtime_alias_without_forwarding_unscoped_credentials(enabled):
    runtime = {
        "HUB_OPENROUTER_API_KEY": "synthetic-openrouter-key",
        "HUB_JEV_ENABLED": enabled,
        "HUB_JEV_CONFIDENCE": "0.8",
    }
    environment = {**runtime, "OPENROUTER_API_KEY": "unscoped-canary", "GITHUB_TOKEN": "github-canary"}
    assert client.runtime_environment(environment) == runtime


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


CONFIG = "sha256:" + "c" * 64


def serve_manifests(monkeypatch, documents):
    requested = []

    def fetch(registry, reference):
        requested.append(reference)
        return documents[reference]

    monkeypatch.setattr(client, "fetch_manifest", fetch)
    return requested


def test_release_resolves_a_single_platform_manifest(monkeypatch):
    serve_manifests(monkeypatch, {"rev": ("sha256:" + "1" * 64, {"config": {"digest": CONFIG}})})
    assert client.resolve_release("127.0.0.1:1", "rev") == ("sha256:" + "1" * 64, CONFIG)


def test_release_index_pins_the_linux_amd64_manifest(monkeypatch):
    index = {
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": [
            {"digest": "sha256:" + "2" * 64, "platform": {"os": "linux", "architecture": "amd64"}},
            {"digest": "sha256:" + "3" * 64, "platform": {"os": "unknown", "architecture": "unknown"}},
        ],
    }
    requested = serve_manifests(
        monkeypatch,
        {"rev": ("sha256:" + "0" * 64, index), "sha256:" + "2" * 64: ("sha256:" + "2" * 64, {"config": {"digest": CONFIG}})},
    )
    assert client.resolve_release("127.0.0.1:1", "rev") == ("sha256:" + "2" * 64, CONFIG)
    assert requested == ["rev", "sha256:" + "2" * 64]


@pytest.mark.parametrize(
    "document",
    [
        {"config": {"digest": "sha256:short"}},
        {"manifests": []},
        {"manifests": [{"digest": "sha256:" + "2" * 64, "platform": {"os": "linux", "architecture": "arm64"}}]},
    ],
)
def test_release_rejects_ambiguous_or_invalid_manifests(monkeypatch, document):
    serve_manifests(monkeypatch, {"rev": ("sha256:" + "0" * 64, document)})
    with pytest.raises(client.PublishError):
        client.resolve_release("127.0.0.1:1", "rev")


def test_publish_pushes_through_the_single_permitted_forward_and_closes_it(monkeypatch):
    commands = []

    class Tunnel:
        terminated = False

        def poll(self):
            return None

        def terminate(self):
            Tunnel.terminated = True

        def wait(self, timeout):
            return 0

    def popen(command, **kwargs):
        commands.append(command)
        return Tunnel()

    def run(command, **kwargs):
        commands.append(command)
        return CompletedProcess(command, 0)

    monkeypatch.setenv("GITHUB_RUN_ID", "77")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    monkeypatch.setattr(client, "free_port", lambda: 41234)
    monkeypatch.setattr(client.subprocess, "Popen", popen)
    monkeypatch.setattr(client.subprocess, "run", run)
    monkeypatch.setattr(client, "wait_for_registry", lambda registry, tunnel: None)
    resolved = []
    monkeypatch.setattr(client, "resolve_release", lambda registry, tag: resolved.append(tag) or ("sha256:" + "1" * 64, CONFIG))
    result = client.publish(["ssh", "-i", "key"], "deploy@host", "msu-hub-bot:" + "a" * 40, "a" * 40)
    assert result == ("sha256:" + "1" * 64, CONFIG)
    tunnel = commands[0]
    assert tunnel[-3:] == ["-L", "127.0.0.1:41234:127.0.0.1:5000", "deploy@host"] and "-N" in tunnel
    assert ["docker", "image", "push", "--quiet", "127.0.0.1:41234/msu-hub-bot:" + "a" * 40 + "-77-2"] in commands
    assert resolved == ["a" * 40 + "-77-2"]
    assert Tunnel.terminated


def test_deploy_requires_the_image_built_for_its_revision(monkeypatch):
    for name, value in {
        "DEPLOY_HOST": "host",
        "DEPLOY_USER": "deploy",
        "GITHUB_SHA": "a" * 40,
        "DEPLOY_IMAGE": "msu-hub-bot:other",
    }.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(SystemExit, match="does not match"):
        client.main()


def test_push_tags_are_unique_per_attempt_and_validated():
    assert client.push_tag("a" * 40, {"GITHUB_RUN_ID": "5", "GITHUB_RUN_ATTEMPT": "3"}) == "a" * 40 + "-5-3"
    with pytest.raises(client.PublishError):
        client.push_tag("a" * 40, {"GITHUB_RUN_ID": "5;rm", "GITHUB_RUN_ATTEMPT": "1"})


def test_registry_status_lines_are_public(capsys):
    client.report_result(CompletedProcess([], 0, stdout="Registry holds 512 MB\nRegistry holds lots\n"))
    assert capsys.readouterr().out == "Registry holds 512 MB\n"
