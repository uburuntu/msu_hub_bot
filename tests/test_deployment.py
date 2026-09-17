import importlib.util
import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location("deployment", Path(__file__).resolve().parents[1] / "tools/deployment/host.py")
deployment = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deployment)


def payload():
    return {
        "action": "deploy",
        "image": "sha256:" + "a" * 64,
        "revision": "b" * 40,
        "environment": {"HUB_BOT_TOKEN": "fake", "HUB_REDIS_HOST": "localhost", "HUB_EDGEDB_DSN": "fake"},
        "archive_sha256": "a" * 64,
        "archive_size": 100,
    }


def supabase_payload():
    request = payload()
    request["environment"].pop("HUB_EDGEDB_DSN")
    request["environment"].update(
        HUB_STORAGE_BACKEND="supabase",
        HUB_SUPABASE_URL="http://database.invalid:8000",
        HUB_SUPABASE_KEY="synthetic-publishable-key",
        HUB_SUPABASE_EMAIL="bot@example.invalid",
        HUB_SUPABASE_PASSWORD="synthetic-password",
    )
    return request


def test_deployment_requires_selected_backend_credentials():
    deployment.validate_payload(payload())
    deployment.validate_payload(supabase_payload())
    request = supabase_payload()
    request["environment"].pop("HUB_SUPABASE_PASSWORD")
    with pytest.raises(deployment.DeploymentError, match="Missing core"):
        deployment.validate_payload(request)
    request = payload()
    request["environment"]["HUB_STORAGE_BACKEND"] = "other"
    with pytest.raises(deployment.DeploymentError, match="Invalid storage backend"):
        deployment.validate_payload(request)


@pytest.mark.parametrize("image", ["elsewhere/bot:latest", "msu-hub-bot:latest", "$(touch /tmp/pwned)"])
def test_rejects_mutable_or_foreign_images(image):
    request = payload()
    request["image"] = image
    with pytest.raises(deployment.DeploymentError):
        deployment.validate_payload(request)


def test_compose_only_manages_the_bot_and_literal_configuration(tmp_path):
    document = deployment.compose_document(payload()["image"], tmp_path / "runtime.env")
    assert set(document["services"]) == {"bot"}
    assert document["networks"] == {"msu_db": {"external": True}}
    assert document["services"]["bot"]["env_file"][0]["format"] == "raw"
    assert "volumes" not in document
    assert "ports" not in document["services"]["bot"]
    assert document["services"]["bot"]["pull_policy"] == "never"


def test_failed_cutover_restores_legacy_after_stopping_replacement(tmp_path):
    class Fake(deployment.Deployer):
        def __init__(self):
            super().__init__(tmp_path)
            self.events = []

        def run(self, *args, **kwargs):
            return ""

        def receive_image(self, *args):
            pass

        def record_container_failure(self):
            pass

        def inspect(self, name):
            return {"State": {"Running": True}, "HostConfig": {"RestartPolicy": {"Name": "unless-stopped"}}}

        def compose(self, state, *args, **kwargs):
            self.events.append(args[0])

        def stop_legacy(self):
            self.events.append("stop_legacy")

        def stop_replacement(self):
            self.events.append("stop_replacement")

        def wait_healthy(self, **kwargs):
            raise deployment.DeploymentError("not ready")

        def restore(self, previous):
            assert previous["legacy"]
            self.stop_replacement()
            self.events.append("start_legacy")

    fake = Fake()
    with pytest.raises(deployment.DeploymentError, match="rolled back"):
        fake.deploy(payload())
    assert fake.events == ["config", "run", "stop_legacy", "stop_replacement", "up", "stop_replacement", "start_legacy"]
    assert not (tmp_path / "current.json").exists()
    runtime = next((tmp_path / "releases").glob("*/runtime.env"))
    assert runtime.stat().st_mode & 0o777 == 0o600
    assert json.loads(runtime.read_text().split("=", 1)[1])["HUB_BOT_TOKEN"] == "fake"


def test_rollback_from_legacy_stops_it_before_starting_an_extracted_release(tmp_path):
    events = []
    deployer = deployment.Deployer(tmp_path)
    deployer.stop_replacement = lambda: events.append("stop_replacement")
    deployer.ensure_image = lambda state: events.append("image_ready")
    deployer.stop_legacy = lambda: events.append("stop_legacy")
    deployer.compose = lambda *args: events.append("start_extracted")
    deployer.wait_healthy = lambda: events.append("ready")
    deployer.restore({"release": "prior"})
    assert events == ["image_ready", "stop_replacement", "stop_legacy", "start_extracted", "ready"]


def test_first_deployment_on_a_fresh_host_and_unavailable_rollback(tmp_path):
    deployer = deployment.Deployer(tmp_path)
    deployer.run = lambda *args, **kwargs: ""
    deployer.inspect = lambda name: None
    deployer.receive_image = lambda *args: None
    deployer.compose = lambda *args, **kwargs: ""
    deployer.wait_healthy = lambda: None
    deployer.deploy(payload())
    assert deployer.read_state("current.json")["image"] == payload()["image"]
    assert deployer.read_state("previous.json") == {"empty": True}
    with pytest.raises(deployment.DeploymentError, match="No prior release"):
        deployer.deploy({"action": "rollback"})


def test_failed_manual_rollback_restores_current_release(tmp_path):
    previous, current = {"release": "old"}, {"release": "current"}
    deployment.write_private(tmp_path / "previous.json", json.dumps(previous))
    deployment.write_private(tmp_path / "current.json", json.dumps(current))
    deployer = deployment.Deployer(tmp_path)
    events = []

    def restore(state):
        events.append(state["release"])
        if state == previous:
            raise deployment.DeploymentError("not ready")

    deployer.restore = restore
    with pytest.raises(deployment.DeploymentError, match="current release restored"):
        deployer.deploy({"action": "rollback"})
    assert events == ["old", "current"]
    assert deployer.read_state("current.json") == current


@pytest.mark.parametrize("previous_backend,current_backend", [(None, "supabase"), ("supabase", "edgedb")])
def test_manual_rollback_cannot_resume_a_different_database_writer(tmp_path, previous_backend, current_backend):
    previous = {"release": "old", **({"storage_backend": previous_backend} if previous_backend else {})}
    current = {"release": "current", "storage_backend": current_backend}
    deployment.write_private(tmp_path / "previous.json", json.dumps(previous))
    deployment.write_private(tmp_path / "current.json", json.dumps(current))
    deployer = deployment.Deployer(tmp_path)
    events = []
    deployer.restore = lambda state: events.append("restore")
    with pytest.raises(deployment.DeploymentError, match="reconcile data"):
        deployer.deploy({"action": "rollback"})
    assert events == []
    assert deployer.read_state("current.json") == current
    assert deployer.read_state("previous.json") == previous


@pytest.mark.parametrize("same_backend", [False, True])
def test_failed_supabase_release_stops_before_backend_guard_or_safe_restore(tmp_path, same_backend):
    previous = {"release": "old"}
    if same_backend:
        previous["storage_backend"] = "supabase"
    deployment.write_private(tmp_path / "current.json", json.dumps(previous))
    deployer = deployment.Deployer(tmp_path)
    events = []
    deployer.run = lambda *args, **kwargs: ""
    deployer.receive_image = lambda *args: None
    deployer.compose = lambda state, *args, **kwargs: events.append(args[0])
    deployer.stop_legacy = lambda: events.append("stop_legacy")
    deployer.stop_replacement = lambda: events.append("stop_replacement")
    deployer.record_container_failure = lambda: events.append("diagnostics")
    deployer.restore = lambda state: events.append("restore")

    def fail():
        raise deployment.DeploymentError("unhealthy")

    deployer.wait_healthy = fail
    expected = "rolled back" if same_backend else "Reconcile data"
    with pytest.raises(deployment.DeploymentError, match=expected):
        deployer.deploy(supabase_payload())
    assert events[:5] == ["config", "run", "stop_legacy", "stop_replacement", "up"]
    assert events[5:] == ["diagnostics", "restore" if same_backend else "stop_replacement"]
    assert deployer.read_state("current.json") == previous
    release = next((tmp_path / "releases").iterdir())
    assert (release / "runtime.env").exists() and (release / "release.json").exists()
    assert json.loads((release / "release.json").read_text())["storage_backend"] == "supabase"


def backend_transition_deployer(tmp_path):
    deployment.write_private(tmp_path / "previous.json", json.dumps({"release": "older"}))
    deployment.write_private(tmp_path / "current.json", json.dumps({"release": "old"}))
    deployer = deployment.Deployer(tmp_path)
    deployer.run = lambda *args, **kwargs: ""
    deployer.receive_image = lambda *args: None
    deployer.compose = lambda *args, **kwargs: None
    deployer.stop_legacy = lambda: None
    deployer.stop_replacement = lambda: None
    deployer.record_container_failure = lambda: None
    deployer.wait_healthy = lambda: None
    return deployer


@pytest.mark.parametrize("followup", [{"action": "rollback"}, payload(), supabase_payload()])
def test_failed_backend_transition_blocks_later_release_requests(tmp_path, followup):
    deployer = backend_transition_deployer(tmp_path)

    def fail():
        raise deployment.DeploymentError("unhealthy")

    deployer.wait_healthy = fail
    with pytest.raises(deployment.DeploymentError, match="Reconcile data"):
        deployer.deploy(supabase_payload())

    later = deployment.Deployer(tmp_path)
    events = []
    later.run = lambda *args, **kwargs: events.append("docker")
    later.restore = lambda *args: events.append("restore")
    with pytest.raises(deployment.DeploymentError, match="Storage transition requires administrative recovery"):
        later.deploy(followup)
    assert events == []


def test_backend_transition_marker_precedes_candidate_and_survives_interruption(tmp_path):
    deployer = backend_transition_deployer(tmp_path)
    marker = tmp_path / "storage-transition.json"

    class Interrupted(BaseException):
        pass

    def start(state, action, *args, **kwargs):
        if action == "up":
            assert json.loads(marker.read_text()) == {"previous": {"release": "old"}, "candidate": state}
            assert marker.stat().st_mode & 0o777 == 0o600
            assert "HUB_" not in marker.read_text()
            raise Interrupted

    deployer.compose = start
    with pytest.raises(Interrupted):
        deployer.deploy(supabase_payload())
    with pytest.raises(deployment.DeploymentError, match="administrative recovery"):
        deployment.Deployer(tmp_path).deploy({"action": "rollback"})


@pytest.mark.parametrize("failed_record", ["previous.json", "current.json"])
def test_backend_transition_stays_blocked_when_release_publication_fails(tmp_path, monkeypatch, failed_record):
    deployer = backend_transition_deployer(tmp_path)
    original = deployment.write_private

    def publish(path, data):
        if path == tmp_path / failed_record:
            raise OSError("synthetic state publication failure")
        original(path, data)

    monkeypatch.setattr(deployment, "write_private", publish)
    with pytest.raises(OSError, match="state publication"):
        deployer.deploy(supabase_payload())
    assert (tmp_path / "storage-transition.json").exists()
    with pytest.raises(deployment.DeploymentError, match="administrative recovery"):
        deployment.Deployer(tmp_path).deploy(supabase_payload())


def test_successful_backend_transition_clears_marker_after_publishing_both_states(tmp_path, monkeypatch):
    deployer = backend_transition_deployer(tmp_path)
    marker = tmp_path / "storage-transition.json"
    published = []
    original = deployment.write_private

    def publish(path, data):
        if path in (tmp_path / "previous.json", tmp_path / "current.json"):
            assert marker.exists()
            published.append(path.name)
        original(path, data)

    monkeypatch.setattr(deployment, "write_private", publish)
    deployer.deploy(supabase_payload())
    assert published == ["previous.json", "current.json"]
    assert not marker.exists()
    assert deployer.read_state("current.json")["storage_backend"] == "supabase"
    assert deployer.read_state("previous.json") == {"release": "old"}


def test_marker_removal_failure_keeps_future_requests_blocked(tmp_path, monkeypatch):
    deployer = backend_transition_deployer(tmp_path)
    marker = tmp_path / "storage-transition.json"
    original = Path.unlink

    def unlink(path, *args, **kwargs):
        if path == marker:
            raise OSError("synthetic marker removal failure")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    with pytest.raises(OSError, match="marker removal"):
        deployer.deploy(supabase_payload())
    assert marker.exists()
    assert deployer.read_state("current.json")["storage_backend"] == "supabase"
    with pytest.raises(deployment.DeploymentError, match="administrative recovery"):
        deployment.Deployer(tmp_path).deploy({"action": "rollback"})


@pytest.mark.parametrize("broken_marker", ["invalid_json", "dangling_symlink"])
def test_storage_transition_marker_blocks_without_parsing_or_docker_work(tmp_path, broken_marker):
    marker = tmp_path / "storage-transition.json"
    if broken_marker == "dangling_symlink":
        marker.symlink_to(tmp_path / "missing.json")
    else:
        marker.write_text("not JSON")
    deployer = deployment.Deployer(tmp_path)
    events = []
    deployer.run = lambda *args, **kwargs: events.append("docker")
    with pytest.raises(deployment.DeploymentError, match="administrative recovery"):
        deployer.deploy(supabase_payload())
    assert events == []
    assert not (tmp_path / "releases").exists()


def make_archive(path, *, foreign_tag=False, traversal=False, contains_env=False, runtime_env=None):
    state = payload()
    config = {
        "architecture": "amd64",
        "os": "linux",
        "config": {
            "User": "10001:10001",
            "Entrypoint": ["/opt/msu_hub_bot/.venv/bin/msu-hub-bot"],
            "Labels": {"org.opencontainers.image.revision": state["revision"]},
            "Env": runtime_env if runtime_env is not None else (["HUB_BOT_TOKEN=synthetic"] if contains_env else []),
        },
    }
    raw = json.dumps(config).encode()
    digest = hashlib.sha256(raw).hexdigest()
    state["image"] = "sha256:" + digest
    manifest = [
        {"Config": digest + ".json", "RepoTags": ["neighbor:latest" if foreign_tag else "msu-hub-bot:" + state["revision"]], "Layers": []}
    ]
    files = {"manifest.json": json.dumps(manifest).encode(), digest + ".json": raw}
    if traversal:
        files["../outside"] = b"unsafe"
    with tarfile.open(path, "w:gz") as archive:
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    state["archive_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    state["archive_size"] = path.stat().st_size
    return state


@pytest.mark.parametrize("bad", [None, "foreign_tag", "traversal", "contains_env", "wrong_image", "truncated"])
def test_image_archive_boundary(tmp_path, bad):
    path = tmp_path / "image.tar.gz"
    state = make_archive(path, **({bad: True} if bad in {"foreign_tag", "traversal", "contains_env"} else {}))
    if bad == "wrong_image":
        state["image"] = "sha256:" + "0" * 64
    if bad == "truncated":
        path.write_bytes(path.read_bytes()[:50])
    if bad is None:
        deployment.validate_archive(path, state)
    else:
        with pytest.raises(deployment.DeploymentError):
            deployment.validate_archive(path, state)


def test_interrupted_upload_removes_partial_image(tmp_path):
    deployer = deployment.Deployer(tmp_path)
    with pytest.raises(deployment.DeploymentError, match="interrupted"):
        deployer.receive_image(payload(), tmp_path, io.BytesIO(b"partial"))
    assert not (tmp_path / "image.tar.gz").exists()


def test_write_token_payload_is_accepted_and_saved_only_in_private_runtime_file(tmp_path):
    request = payload()
    request["environment"]["LOGFIRE_TOKEN"] = "write-token-canary"
    deployment.validate_payload(request)
    deployer = deployment.Deployer(tmp_path)
    deployer.run = lambda *args, **kwargs: ""
    deployer.inspect = lambda name: None
    deployer.receive_image = lambda *args: None
    deployer.compose = lambda *args, **kwargs: ""
    deployer.wait_healthy = lambda: None
    deployer.deploy(request)
    runtime = next((tmp_path / "releases").glob("*/runtime.env"))
    assert runtime.stat().st_mode & 0o777 == 0o600
    assert json.loads(runtime.read_text().split("=", 1)[1])["LOGFIRE_TOKEN"] == "write-token-canary"
    assert "write-token-canary" not in (tmp_path / "current.json").read_text()


@pytest.mark.parametrize("key", ["LOGFIRE_API_KEY", "LOGFIRE_READ_TOKEN", "LOGFIRE_TOKEN_EXTRA", "OTEL_EXPORTER_OTLP_HEADERS"])
def test_host_rejects_management_and_arbitrary_telemetry_variables(key):
    request = payload()
    request["environment"][key] = "private-canary"
    with pytest.raises(deployment.DeploymentError, match="Invalid runtime configuration") as caught:
        deployment.validate_payload(request)
    assert "private-canary" not in str(caught.value)


@pytest.mark.parametrize("variable", ["LOGFIRE_TOKEN", "LOGFIRE_API_KEY", "LOGFIRE_READ_TOKEN", "OTEL_EXPORTER_OTLP_HEADERS"])
def test_archive_rejects_baked_telemetry_configuration(tmp_path, variable):
    path = tmp_path / "image.tar.gz"
    state = make_archive(path, runtime_env=[variable + "=private-canary"])
    with pytest.raises(deployment.DeploymentError, match="Image contains runtime configuration") as caught:
        deployment.validate_archive(path, state)
    assert "private-canary" not in str(caught.value)
