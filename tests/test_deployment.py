import importlib.util
import hashlib
import io
import json
import os
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
        "environment": {
            "HUB_BOT_TOKEN": "fake",
            "HUB_REDIS_HOST": "localhost",
            "HUB_STORAGE_BACKEND": "supabase",
            "HUB_SUPABASE_URL": "http://database.invalid:8000",
            "HUB_SUPABASE_KEY": "synthetic-publishable-key",
            "HUB_SUPABASE_EMAIL": "bot@example.invalid",
            "HUB_SUPABASE_PASSWORD": "synthetic-password",
            "HUB_SUPABASE_SCHEMA": "msu_hub_api",
        },
        "archive_sha256": "a" * 64,
        "archive_size": 100,
    }


def supabase_payload(schema="msu_hub_api"):
    request = payload()
    request["environment"]["HUB_SUPABASE_SCHEMA"] = schema
    return request


def stored_supabase(tmp_path, *, schema="msu_hub_api", number=1, historical=False, omit_schema=False):
    request = supabase_payload(schema)
    state = {
        "release": request["revision"] + f"-{number}",
        "storage_backend": "supabase",
        **{key: request[key] for key in ("revision", "image", "archive_sha256", "archive_size")},
    }
    if not historical:
        state["supabase_schema"] = schema
    if omit_schema:
        request["environment"].pop("HUB_SUPABASE_SCHEMA")
    directory = tmp_path / "releases" / state["release"]
    deployment.write_private(directory / "release.json", json.dumps(state))
    deployment.write_private(directory / "runtime.env", "HUB_CONFIG_JSON=" + json.dumps(request["environment"]) + "\n")
    return state


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


@pytest.mark.parametrize("schema", [None, "", "hub-api", "Hub_Api", "a" * 64, "hub_api; private-canary", "$(private-canary)"])
def test_new_supabase_payload_requires_an_explicit_schema(schema):
    request = supabase_payload()
    if schema is None:
        request["environment"].pop("HUB_SUPABASE_SCHEMA")
    else:
        request["environment"]["HUB_SUPABASE_SCHEMA"] = schema
    with pytest.raises(deployment.DeploymentError, match="Supabase schema") as caught:
        deployment.validate_payload(request)
    assert "private-canary" not in str(caught.value)


@pytest.mark.parametrize("schema,omit_schema", [("hub_api", True), ("hub_api", False), ("msu_hub_api", False)])
def test_historical_supabase_identity_comes_from_the_private_runtime_file(tmp_path, schema, omit_schema):
    state = stored_supabase(tmp_path, schema=schema, historical=True, omit_schema=omit_schema)
    assert deployment.Deployer(tmp_path).storage_identity(state) == ("supabase", schema, "relational-v1")


def test_new_metadata_does_not_enable_the_historical_schema_default(tmp_path):
    state = stored_supabase(tmp_path, schema="hub_api", omit_schema=True)
    with pytest.raises(deployment.DeploymentError, match="Invalid protected release configuration"):
        deployment.Deployer(tmp_path).storage_identity(state)


@pytest.mark.parametrize("release", ["../private-canary", "/private-canary", "b" * 40 + "-1/child", "not-a-release"])
def test_historical_storage_identity_rejects_unsafe_release_paths(tmp_path, release):
    state = stored_supabase(tmp_path, historical=True)
    state["release"] = release
    with pytest.raises(deployment.DeploymentError, match="Invalid protected release configuration") as caught:
        deployment.Deployer(tmp_path).storage_identity(state)
    assert "private-canary" not in str(caught.value)


@pytest.mark.parametrize("target", ["runtime.env", "release.json", "release_directory", "releases_parent"])
def test_storage_identity_does_not_follow_symlinks(tmp_path, target):
    state = stored_supabase(tmp_path, historical=True)
    directory = tmp_path / "releases" / state["release"]
    path = directory if target == "release_directory" else directory.parent if target == "releases_parent" else directory / target
    destination = tmp_path / "private-canary"
    path.rename(destination)
    path.symlink_to(destination)
    with pytest.raises(deployment.DeploymentError, match="Invalid protected release configuration") as caught:
        deployment.Deployer(tmp_path).storage_identity(state)
    assert "private-canary" not in str(caught.value)


@pytest.mark.parametrize(
    "target,mode", [("runtime.env", 0o640), ("release.json", 0o644), ("release_directory", 0o750), ("releases_parent", 0o777)]
)
def test_storage_identity_requires_protected_permissions(tmp_path, target, mode):
    state = stored_supabase(tmp_path)
    directory = tmp_path / "releases" / state["release"]
    path = directory if target == "release_directory" else directory.parent if target == "releases_parent" else directory / target
    path.chmod(mode)
    with pytest.raises(deployment.DeploymentError, match="Invalid protected release configuration"):
        deployment.Deployer(tmp_path).storage_identity(state)


def test_storage_identity_rejects_releases_owned_by_a_different_account(tmp_path, monkeypatch):
    state = stored_supabase(tmp_path)
    monkeypatch.setattr(deployment.os, "geteuid", lambda: tmp_path.stat().st_uid + 1)
    with pytest.raises(deployment.DeploymentError, match="Invalid protected release configuration"):
        deployment.Deployer(tmp_path).storage_identity(state)


@pytest.mark.parametrize(
    "damage",
    [
        "envelope",
        "extra_line",
        "malformed",
        "duplicate",
        "nested",
        "nonstrings",
        "oversized",
        "non_utf8",
        "fifo",
        "directory",
        "backend",
        "schema",
        "metadata",
    ],
)
def test_storage_identity_rejects_malformed_or_inconsistent_configuration_without_leaking(tmp_path, capsys, damage):
    state = stored_supabase(tmp_path)
    path = tmp_path / "releases" / state["release"] / "runtime.env"
    text = path.read_text()
    environment = json.loads(text.split("=", 1)[1])
    environment["LOGFIRE_TOKEN"] = "private-canary"
    if damage == "envelope":
        path.write_text(json.dumps(environment))
    elif damage == "extra_line":
        path.write_text(text + "OTHER=private-canary\n")
    elif damage == "malformed":
        path.write_text("HUB_CONFIG_JSON={private-canary\n")
    elif damage == "duplicate":
        path.write_text('HUB_CONFIG_JSON={"HUB_BOT_TOKEN":"private-canary","HUB_BOT_TOKEN":"duplicate"}\n')
    elif damage == "oversized":
        path.write_text("private-canary" * deployment.MAX_CONFIGURATION_SIZE)
    elif damage == "non_utf8":
        path.write_bytes(b"private-canary\xff")
    elif damage in {"fifo", "directory"}:
        path.unlink()
        os.mkfifo(path, 0o600) if damage == "fifo" else path.mkdir(mode=0o700)
    elif damage == "metadata":
        state["image"] = "private-canary"
    else:
        if damage == "nested":
            environment["HUB_CONFIG_JSON"] = "private-canary"
        elif damage == "nonstrings":
            environment["LOGFIRE_TOKEN"] = {"private-canary": 1}
        elif damage == "backend":
            environment.update(HUB_STORAGE_BACKEND="edgedb", HUB_EDGEDB_DSN="private-canary")
        elif damage == "schema":
            environment["HUB_SUPABASE_SCHEMA"] = "other_api"
        path.write_text("HUB_CONFIG_JSON=" + json.dumps(environment) + "\n")
    with pytest.raises(deployment.DeploymentError, match="Invalid protected release configuration") as caught:
        deployment.Deployer(tmp_path).storage_identity(state)
    assert "private-canary" not in str(caught.value)
    assert capsys.readouterr() == ("", "")


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


def test_generated_release_bounds_cpu_memory_processes_and_temporary_storage(tmp_path):
    bot = deployment.compose_document(payload()["image"], tmp_path / "runtime.env")["services"]["bot"]
    assert bot["cpus"] == 3.0
    assert bot["mem_limit"] == bot["memswap_limit"] == "4g"
    assert bot["pids_limit"] == 128
    assert bot["init"] is True
    assert bot["read_only"] is True
    assert set(bot["tmpfs"]) == {"/tmp:mode=1777,size=512m", "/work:mode=1777,size=512m"}


def test_failed_supabase_cutover_never_resumes_a_retired_database_writer(tmp_path):
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
    with pytest.raises(deployment.DeploymentError, match="storage identity change"):
        fake.deploy(payload())
    assert fake.events == ["config", "run", "stop_legacy", "stop_replacement", "up", "stop_replacement"]
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
    previous, current = stored_supabase(tmp_path), stored_supabase(tmp_path, number=2)
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
    assert events == [previous["release"], current["release"]]
    assert deployer.read_state("current.json") == current


@pytest.mark.parametrize("metadata", [{}, {"storage_backend": "edgedb"}, {"legacy": True, "restart_policy": "unless-stopped"}])
def test_manual_rollback_cannot_resume_matching_retired_backends(tmp_path, monkeypatch, metadata):
    previous, current = {"release": "old", **metadata}, {"release": "current", **metadata}
    deployment.write_private(tmp_path / "previous.json", json.dumps(previous))
    deployment.write_private(tmp_path / "current.json", json.dumps(current))
    deployer = deployment.Deployer(tmp_path)
    events = []
    deployer.run = lambda *args, **kwargs: events.append("docker")
    deployer.restore = lambda state: events.append("restore")
    monkeypatch.setattr(deployment, "write_private", lambda *args: events.append("write"))
    with pytest.raises(deployment.DeploymentError, match="cannot resume a retired database writer"):
        deployer.deploy({"action": "rollback"})
    assert events == []
    assert deployer.read_state("current.json") == current
    assert deployer.read_state("previous.json") == previous


@pytest.mark.parametrize("previous_backend,current_backend", [(None, "supabase"), ("supabase", "edgedb")])
def test_manual_rollback_cannot_resume_a_different_database_writer(tmp_path, previous_backend, current_backend):
    previous = {"release": "old", **({"storage_backend": previous_backend} if previous_backend else {})}
    current = {"release": "current", "storage_backend": current_backend}
    if previous_backend == "supabase":
        previous = stored_supabase(tmp_path)
    if current_backend == "supabase":
        current = stored_supabase(tmp_path)
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


@pytest.mark.parametrize("same_namespace", [False, True])
def test_manual_rollback_checks_namespace_even_when_historical_metadata_omits_it(tmp_path, same_namespace):
    previous = stored_supabase(
        tmp_path, schema="msu_hub_api" if same_namespace else "hub_api", historical=True, omit_schema=not same_namespace
    )
    current = stored_supabase(tmp_path, number=2)
    deployment.write_private(tmp_path / "previous.json", json.dumps(previous))
    deployment.write_private(tmp_path / "current.json", json.dumps(current))
    deployer = deployment.Deployer(tmp_path)
    restored = []
    deployer.restore = restored.append
    if same_namespace:
        deployer.deploy({"action": "rollback"})
        assert restored == [previous]
        assert deployer.read_state("current.json") == previous
        assert deployer.read_state("previous.json") == current
    else:
        with pytest.raises(deployment.DeploymentError, match="Supabase schema; reconcile data"):
            deployer.deploy({"action": "rollback"})
        assert not restored
        assert deployer.read_state("current.json") == current
        assert deployer.read_state("previous.json") == previous


@pytest.mark.parametrize("same_backend", [False, True])
def test_failed_supabase_release_stops_before_backend_guard_or_safe_restore(tmp_path, same_backend):
    previous = {"release": "old"}
    if same_backend:
        previous = stored_supabase(tmp_path)
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
    assert json.loads((release / "release.json").read_text())["supabase_schema"] == "msu_hub_api"


def backend_transition_deployer(tmp_path, namespace=False):
    deployment.write_private(tmp_path / "previous.json", json.dumps({"release": "older"}))
    previous = stored_supabase(tmp_path, schema="hub_api", historical=True, omit_schema=True) if namespace else {"release": "old"}
    deployment.write_private(tmp_path / "current.json", json.dumps(previous))
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
@pytest.mark.parametrize("namespace", [False, True])
def test_failed_storage_transition_blocks_later_release_requests(tmp_path, followup, namespace):
    deployer = backend_transition_deployer(tmp_path, namespace)

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


@pytest.mark.parametrize("namespace", [False, True])
def test_storage_transition_marker_precedes_candidate_and_survives_interruption(tmp_path, namespace):
    deployer = backend_transition_deployer(tmp_path, namespace)
    previous = deployer.read_state("current.json")
    marker = tmp_path / "storage-transition.json"

    class Interrupted(BaseException):
        pass

    def start(state, action, *args, **kwargs):
        if action == "up":
            assert json.loads(marker.read_text()) == {"previous": previous, "candidate": state}
            assert marker.stat().st_mode & 0o777 == 0o600
            assert "HUB_" not in marker.read_text()
            raise Interrupted

    deployer.compose = start
    with pytest.raises(Interrupted):
        deployer.deploy(supabase_payload())
    with pytest.raises(deployment.DeploymentError, match="administrative recovery"):
        deployment.Deployer(tmp_path).deploy({"action": "rollback"})


@pytest.mark.parametrize("failed_record", ["previous.json", "current.json"])
@pytest.mark.parametrize("namespace", [False, True])
def test_storage_transition_stays_blocked_when_release_publication_fails(tmp_path, monkeypatch, failed_record, namespace):
    deployer = backend_transition_deployer(tmp_path, namespace)
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


def test_namespace_transition_does_not_stop_the_poller_if_the_guard_cannot_be_saved(tmp_path, monkeypatch):
    deployer = backend_transition_deployer(tmp_path, namespace=True)
    original = deployment.write_private
    stops = []
    deployer.stop_legacy = lambda: stops.append("legacy")
    deployer.stop_replacement = lambda: stops.append("replacement")

    def publish(path, data):
        if path == tmp_path / "storage-transition.json":
            raise OSError("synthetic guard publication failure")
        original(path, data)

    monkeypatch.setattr(deployment, "write_private", publish)
    with pytest.raises(OSError, match="guard publication"):
        deployer.deploy(supabase_payload())
    assert not stops
    assert deployer.storage_identity(deployer.read_state("current.json")) == ("supabase", "hub_api", "relational-v1")


@pytest.mark.parametrize("namespace", [False, True])
def test_successful_storage_transition_clears_marker_after_publishing_both_states(tmp_path, monkeypatch, namespace):
    deployer = backend_transition_deployer(tmp_path, namespace)
    previous = deployer.read_state("current.json")
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
    assert deployer.read_state("previous.json") == previous


@pytest.mark.parametrize("namespace", [False, True])
def test_marker_removal_failure_keeps_future_requests_blocked(tmp_path, monkeypatch, namespace):
    deployer = backend_transition_deployer(tmp_path, namespace)
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


def test_interrupted_upload_removes_partial_image(tmp_path, monkeypatch):
    monkeypatch.setattr(deployment.shutil, "disk_usage", lambda path: type("Usage", (), {"free": 10 * 1024**3})())
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


def test_web_release_attaches_only_private_proxy_network_without_host_ports(tmp_path):
    document = deployment.compose_document(payload()["image"], tmp_path / "runtime.env", web=True)
    assert document["networks"] == {"msu_db": {"external": True}, "msu_hub_web": {"external": True}}
    assert document["services"]["bot"]["networks"] == ["msu_db", "msu_hub_web"]
    assert "ports" not in document["services"]["bot"]


def test_rollback_refuses_old_table_writers_after_application_document_cutover(tmp_path):
    previous = stored_supabase(tmp_path)
    current = stored_supabase(tmp_path, number=2)
    runtime = tmp_path / "releases" / current["release"] / "runtime.env"
    environment = json.loads(runtime.read_text().removeprefix("HUB_CONFIG_JSON="))
    environment["HUB_STORAGE_CONTRACT"] = "application-documents-v1"
    deployment.write_private(runtime, "HUB_CONFIG_JSON=" + json.dumps(environment) + "\n")
    deployment.write_private(tmp_path / "current.json", json.dumps(current))
    deployment.write_private(tmp_path / "previous.json", json.dumps(previous))
    deployer = deployment.Deployer(tmp_path)
    restored = []
    deployer.restore = restored.append
    with pytest.raises(deployment.DeploymentError, match="reconcile data"):
        deployer.deploy({"action": "rollback"})
    assert not restored
    assert deployer.read_state("current.json") == current


def administrative_cutover(tmp_path):
    previous = stored_supabase(tmp_path)
    deployment.write_private(tmp_path / "current.json", json.dumps(previous))
    request = supabase_payload()
    request["environment"]["HUB_STORAGE_CONTRACT"] = "application-documents-v1"
    marker = {
        "previous": previous,
        "candidate": {key: request[key] for key in ("image", "revision", "archive_sha256", "archive_size")},
        "administrative_cutover": True,
    }
    deployment.write_private(tmp_path / "storage-transition.json", json.dumps(marker))
    deployer = deployment.Deployer(tmp_path)
    deployer.run = lambda *args, **kwargs: ""
    deployer.receive_image = lambda *args: None
    deployer.compose = lambda *args, **kwargs: None
    deployer.stop_legacy = lambda: None
    deployer.stop_replacement = lambda: None
    deployer.wait_healthy = lambda: None
    deployer.prune_releases = lambda: None
    return deployer, request, marker


@pytest.mark.parametrize("stage", ["receive", "preflight", "start"])
def test_administrative_handoff_never_removes_fence_before_health(tmp_path, stage):
    deployer, request, marker = administrative_cutover(tmp_path)
    fence = tmp_path / "storage-transition.json"

    class Interrupted(BaseException):
        pass

    def interrupt(*args, **kwargs):
        assert fence.exists()
        raise Interrupted

    if stage == "receive":
        deployer.receive_image = interrupt
    else:

        def compose(state, action, *args, **kwargs):
            if action == ("run" if stage == "preflight" else "up"):
                interrupt()

        deployer.compose = compose
    with pytest.raises(Interrupted):
        deployer.deploy(request, adopt_transition=marker)
    with pytest.raises(deployment.DeploymentError, match="administrative recovery"):
        deployment.Deployer(tmp_path).deploy({"action": "rollback"})


def test_administrative_handoff_publishes_before_clearing_fence(tmp_path):
    deployer, request, marker = administrative_cutover(tmp_path)
    fence = tmp_path / "storage-transition.json"
    deployer.wait_healthy = lambda: fence.exists() or pytest.fail("Fence vanished before health")
    deployer.deploy(request, adopt_transition=marker)
    assert not fence.exists()
    assert deployer.read_state("current.json")["revision"] == request["revision"]
    assert deployer.read_state("previous.json") == marker["previous"]


@pytest.mark.parametrize("damage", ["candidate", "previous", "absent", "permissions"])
def test_administrative_handoff_rejects_mismatched_or_unsafe_marker(tmp_path, damage):
    deployer, request, marker = administrative_cutover(tmp_path)
    fence = tmp_path / "storage-transition.json"
    if damage == "candidate":
        marker = {**marker, "candidate": {**marker["candidate"], "image": "sha256:" + "f" * 64}}
    elif damage == "previous":
        marker = {**marker, "previous": {"empty": True}}
    elif damage == "absent":
        fence.unlink()
    else:
        fence.chmod(0o644)
    deployer.run = lambda *args, **kwargs: pytest.fail("Docker reached before fence validation")
    with pytest.raises(deployment.DeploymentError, match="Administrative transition"):
        deployer.deploy(request, adopt_transition=marker)
