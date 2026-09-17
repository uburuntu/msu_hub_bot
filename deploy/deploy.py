#!/usr/bin/env python3
"""Restricted SSH entrypoint. Its only authority is this bot's release operation.

Install under ~/msu_hub_bot, with this exact file as the SSH forced command.
It never evaluates SSH_ORIGINAL_COMMAND or accepts shell commands or host paths.
"""

import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path, PurePosixPath

IMAGE_RE = re.compile(r"sha256:[0-9a-f]{64}")
REVISION_RE = re.compile(r"[0-9a-f]{40}")
CONTAINER = "msu_hub_bot"
LEGACY = "hub_bot"
MAX_ARCHIVE_SIZE = 2 * 1024**3


class DeploymentError(Exception):
    pass


def report(message):
    try:
        print(message, flush=True)
    except OSError:
        # A disconnected CI runner must not interrupt cutover or rollback.
        pass


def validate_payload(payload):
    if not isinstance(payload, dict) or payload.get("action") not in {"deploy", "rollback"}:
        raise DeploymentError("Unknown operation")
    if payload["action"] == "rollback":
        if set(payload) != {"action"}:
            raise DeploymentError("Unexpected rollback fields")
        return
    if set(payload) != {"action", "image", "revision", "environment", "archive_sha256", "archive_size"}:
        raise DeploymentError("Unexpected deployment fields")
    if not isinstance(payload["image"], str) or not IMAGE_RE.fullmatch(payload["image"]):
        raise DeploymentError("Image must be an immutable SHA256 image ID")
    if not isinstance(payload["revision"], str) or not REVISION_RE.fullmatch(payload["revision"]):
        raise DeploymentError("Invalid source revision")
    values = payload["environment"]
    if not isinstance(values, dict) or not values:
        raise DeploymentError("Missing runtime configuration")
    for key, value in values.items():
        if (
            not isinstance(key, str)
            or not (re.fullmatch(r"HUB_[A-Z0-9_]+", key) or key == "LOGFIRE_TOKEN")
            or key == "HUB_CONFIG_JSON"
            or not isinstance(value, str)
            or "\0" in value
        ):
            raise DeploymentError("Invalid runtime configuration")
    backend = values.get("HUB_STORAGE_BACKEND", "edgedb")
    if backend not in {"edgedb", "supabase"}:
        raise DeploymentError("Invalid storage backend")
    database_fields = (
        ("HUB_EDGEDB_DSN",)
        if backend == "edgedb"
        else ("HUB_SUPABASE_URL", "HUB_SUPABASE_KEY", "HUB_SUPABASE_EMAIL", "HUB_SUPABASE_PASSWORD")
    )
    if not all(values.get(key) for key in ("HUB_BOT_TOKEN", "HUB_REDIS_HOST", *database_fields)):
        raise DeploymentError("Missing core runtime settings")
    if not isinstance(payload["archive_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", payload["archive_sha256"]):
        raise DeploymentError("Invalid archive checksum")
    if type(payload["archive_size"]) is not int or not 0 < payload["archive_size"] <= MAX_ARCHIVE_SIZE:
        raise DeploymentError("Invalid archive size")


def validate_archive(path, state):
    with path.open("rb") as source:
        digest = hashlib.sha256()
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != state["archive_sha256"]:
        raise DeploymentError("Image archive checksum mismatch")
    with tarfile.open(path, "r:gz") as archive:
        members, total = {}, 0
        for member in archive:
            name = PurePosixPath(member.name)
            if name.is_absolute() or ".." in name.parts or not (member.isfile() or member.isdir()) or member.name in members:
                raise DeploymentError("Unsafe image archive entry")
            total += member.size
            if total > 8 * 1024**3 or len(members) >= 4096:
                raise DeploymentError("Expanded image archive is too large")
            members[member.name] = member

        def read_json(name):
            member = members.get(name)
            if not member or not member.isfile() or member.size > 1024 * 1024:
                raise DeploymentError("Invalid image archive metadata")
            data = archive.extractfile(member).read()
            return data, json.loads(data)

        _, manifests = read_json("manifest.json")
        if not isinstance(manifests, list) or len(manifests) != 1:
            raise DeploymentError("Archive must contain exactly one image")
        manifest = manifests[0]
        tag = "msu-hub-bot:" + state["revision"]
        if manifest.get("RepoTags") not in ([tag], ["docker.io/library/" + tag]):
            raise DeploymentError("Archive contains a foreign image tag")
        raw, config = read_json(manifest["Config"])
        if "sha256:" + hashlib.sha256(raw).hexdigest() != state["image"]:
            raise DeploymentError("Image ID does not match its configuration")
        if config.get("os") != "linux" or config.get("architecture") != "amd64":
            raise DeploymentError("Unsupported image platform")
        runtime = config.get("config", {})
        if runtime.get("Labels", {}).get("org.opencontainers.image.revision") != state["revision"]:
            raise DeploymentError("Image source revision mismatch")
        if runtime.get("User") != "10001:10001" or runtime.get("Entrypoint") != ["/opt/msu_hub_bot/.venv/bin/msu-hub-bot"]:
            raise DeploymentError("Image does not match this service's runtime contract")
        if any(
            value.startswith(("HUB_", "DEPLOY_", "LOGFIRE_", "OTEL_", "GH_TOKEN=", "GITHUB_TOKEN=")) for value in runtime.get("Env", [])
        ):
            raise DeploymentError("Image contains runtime configuration")
        if not isinstance(manifest.get("Layers"), list) or any(
            layer not in members or not members[layer].isfile() for layer in manifest["Layers"]
        ):
            raise DeploymentError("Invalid image layers")


def write_private(path, data):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as f:
        f.write(data)
        os.fchmod(f.fileno(), 0o600)
        staging = Path(f.name)
    staging.replace(path)


def compose_document(image, env_path):
    return {
        "services": {
            "bot": {
                "image": image,
                "pull_policy": "never",
                "container_name": CONTAINER,
                "env_file": [{"path": str(env_path), "format": "raw"}],
                "restart": "unless-stopped",
                "networks": ["msu_db"],
                "read_only": True,
                "tmpfs": ["/tmp:mode=1777", "/work:mode=1777"],
                "security_opt": ["no-new-privileges:true"],
                "cap_drop": ["ALL"],
                "ulimits": {"core": 0},
                "stop_grace_period": "90s",
                "logging": {"driver": "json-file", "options": {"max-size": "10m", "max-file": "3"}},
            }
        },
        "networks": {"msu_db": {"external": True}},
    }


class Deployer:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.diagnostic_path = None

    def run(self, *args, input=None, timeout=180, check=True):
        try:
            result = subprocess.run(args, input=input, text=True, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise DeploymentError("Operation timed out") from None
        if check and result.returncode:
            # Docker/Compose errors can quote configuration: keep raw output private.
            if self.diagnostic_path:
                write_private(self.diagnostic_path, (result.stdout + result.stderr)[-131072:])
            raise DeploymentError("Docker operation failed")
        return result.stdout if result.returncode == 0 else None

    def inspect(self, name):
        data = self.run("docker", "inspect", name, check=False)
        return json.loads(data)[0] if data else None

    def read_state(self, filename):
        path = self.root / filename
        return json.loads(path.read_text()) if path.exists() else None

    def receive_image(self, payload, directory, stream):
        archive = directory / "image.tar.gz"
        remaining = payload["archive_size"]
        if shutil.disk_usage(directory).free < remaining + 1024**3:
            raise DeploymentError("Insufficient space for image transfer")
        try:
            with archive.open("xb") as output:
                os.fchmod(output.fileno(), 0o600)
                while remaining:
                    chunk = stream.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise DeploymentError("Image transfer was interrupted")
                    output.write(chunk)
                    remaining -= len(chunk)
            validate_archive(archive, payload)
        except Exception:
            archive.unlink(missing_ok=True)
            raise
        self.run("docker", "image", "load", "--input", str(archive), timeout=600)
        if not self.run("docker", "image", "inspect", payload["image"], check=False):
            raise DeploymentError("Transferred image was not loaded")

    def ensure_image(self, state):
        if self.run("docker", "image", "inspect", state["image"], check=False):
            return
        archive = self.root / "releases" / state["release"] / "image.tar.gz"
        validate_archive(archive, state)
        self.run("docker", "image", "load", "--input", str(archive), timeout=600)

    def record_container_failure(self):
        result = subprocess.run(["docker", "logs", "--tail", "200", CONTAINER], capture_output=True, text=True, timeout=20)
        if self.diagnostic_path:
            write_private(self.diagnostic_path, (result.stdout + result.stderr)[-131072:])

    def prune_releases(self):
        keep = [self.read_state(name) for name in ("current.json", "previous.json")]
        directories = {state["release"] for state in keep if state and "release" in state}
        images = {state["image"] for state in keep if state and "image" in state}
        for directory in (self.root / "releases").iterdir():
            if directory.name in directories or directory.is_symlink() or not re.fullmatch(r"[0-9a-f]{40}-[0-9]+", directory.name):
                continue
            metadata = directory / "release.json"
            if metadata.exists():
                old = json.loads(metadata.read_text())
                if IMAGE_RE.fullmatch(old.get("image", "")) and old["image"] not in images:
                    self.run("docker", "image", "rm", old["image"], check=False)
            shutil.rmtree(directory)

    def compose(self, state, *args, timeout=180):
        directory = (self.root / "releases" / state["release"]).resolve()
        if directory.parent != self.root / "releases":
            raise DeploymentError("Invalid stored release path")
        return self.run("docker", "compose", "--project-name", CONTAINER, "--file", str(directory / "compose.json"), *args, timeout=timeout)

    def stop_replacement(self):
        if self.inspect(CONTAINER):
            self.run("docker", "stop", "--time", "90", CONTAINER, timeout=110)
            self.run("docker", "rm", CONTAINER)

    def stop_legacy(self):
        info = self.inspect(LEGACY)
        if not info or not info["State"]["Running"]:
            return
        self.run("docker", "update", "--restart=no", LEGACY)
        # The legacy shell entrypoint does not forward signals to Python.
        signal_python = """import os,signal
from pathlib import Path
for path in Path('/proc').iterdir():
    if not path.name.isdigit() or int(path.name)==os.getpid():
        continue
    try:
        args=(path/'cmdline').read_bytes().split(b'\\0')
        if any(arg==b'main.py' or arg.endswith(b'/main.py') for arg in args):
            os.kill(int(path.name),signal.SIGTERM)
    except (OSError,ProcessLookupError):
        pass
"""
        self.run("docker", "exec", "-i", LEGACY, "python3", "-", input=signal_python, check=False)
        self.run("docker", "stop", "--time", "90", LEGACY, timeout=110)

    def wait_healthy(self, deadline=300):
        until = time.monotonic() + deadline
        while time.monotonic() < until:
            info = self.inspect(CONTAINER)
            if info:
                state = info["State"]
                if state.get("Health", {}).get("Status") == "healthy":
                    if info.get("RestartCount", 0):
                        raise DeploymentError("Replacement restarted during startup")
                    return
                if not state["Running"] or state.get("Health", {}).get("Status") == "unhealthy":
                    break
            time.sleep(3)
        raise DeploymentError("Replacement did not become ready")

    def restore(self, previous):
        if not previous.get("empty") and not previous.get("legacy"):
            self.ensure_image(previous)
        self.stop_replacement()
        if previous.get("empty"):
            return
        if previous.get("legacy"):
            self.run("docker", "update", "--restart=" + previous["restart_policy"], LEGACY)
            self.run("docker", "start", LEGACY)
            if not self.inspect(LEGACY)["State"]["Running"]:
                raise DeploymentError("Legacy container did not restart")
        else:
            self.stop_legacy()
            self.compose(previous, "up", "--detach", "--no-deps", "bot")
            self.wait_healthy()

    def deploy(self, payload, stream=None):
        validate_payload(payload)
        if payload["action"] == "rollback":
            previous, current = self.read_state("previous.json"), self.read_state("current.json")
            if not previous or previous.get("empty") or not current:
                raise DeploymentError("No prior release recorded")
            if previous.get("storage_backend", "edgedb") != current.get("storage_backend", "edgedb"):
                raise DeploymentError("Rollback changes the storage backend; reconcile data before restoring a release")
            try:
                self.restore(previous)
            except Exception:
                report("Rollback failed; restoring the current release")
                self.restore(current)
                raise DeploymentError("Rollback failed; current release restored") from None
            write_private(self.root / "current.json", json.dumps(previous))
            write_private(self.root / "previous.json", json.dumps(current))
            report("Rollback completed")
            return
        self.run("docker", "network", "inspect", "msu_db")
        release = payload["revision"] + "-" + str(time.time_ns())
        directory = self.root / "releases" / release
        directory.mkdir(mode=0o700, parents=True)
        self.diagnostic_path = directory / "failure.log"
        environment = dict(payload["environment"])
        environment["HUB_LOGS_FILE"] = "/tmp/msu_hub_bot.log"
        write_private(directory / "runtime.env", "HUB_CONFIG_JSON=" + json.dumps(environment, ensure_ascii=True) + "\n")
        write_private(directory / "compose.json", json.dumps(compose_document(payload["image"], directory / "runtime.env"), indent=2))
        state = {
            "release": release,
            "storage_backend": environment.get("HUB_STORAGE_BACKEND", "edgedb"),
            **{key: payload[key] for key in ("revision", "image", "archive_sha256", "archive_size")},
        }
        write_private(directory / "release.json", json.dumps(state))
        self.receive_image(payload, directory, stream)
        report("Image received; checking configuration and connections")
        self.compose(state, "config", "--quiet")
        self.compose(
            state,
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "--entrypoint",
            "/opt/msu_hub_bot/.venv/bin/python",
            "bot",
            "-m",
            "msu_hub_bot.preflight",
            timeout=180,
        )
        previous = self.read_state("current.json")
        if previous is None:
            legacy = self.inspect(LEGACY)
            previous = {"legacy": True, "restart_policy": legacy["HostConfig"]["RestartPolicy"]["Name"]} if legacy else {"empty": True}
        report("Preflight passed; stopping the current poller")
        try:
            self.stop_legacy()
            self.stop_replacement()
            self.compose(state, "up", "--detach", "--no-deps", "bot")
            self.wait_healthy()
        except Exception:
            try:
                self.record_container_failure()
            except Exception:
                pass
            if not previous.get("empty") and previous.get("storage_backend", "edgedb") != state["storage_backend"]:
                self.stop_replacement()
                raise DeploymentError(
                    "Release failed after a storage-backend change; poller stopped. Reconcile data before restoring either release"
                ) from None
            report("Release failed; restoring the previous poller")
            self.restore(previous)
            report("Previous poller restored")
            raise DeploymentError("Release failed and was rolled back") from None
        write_private(self.root / "previous.json", json.dumps(previous))
        write_private(self.root / "current.json", json.dumps(state))
        report("Deployed " + payload["revision"] + " " + payload["image"])
        try:
            self.prune_releases()
        except Exception:
            # Cleanup must not turn a healthy release into a failed deployment.
            pass


def main():
    os.umask(0o077)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    root = Path(__file__).resolve().parent
    data = sys.stdin.buffer.readline(131_073)
    if len(data) > 131_072:
        raise DeploymentError("Deployment request is too large")
    try:
        payload = json.loads(data)
    except ValueError:
        raise DeploymentError("Invalid deployment request") from None
    validate_payload(payload)
    with (root / "deployment.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        Deployer(root).deploy(payload, sys.stdin.buffer)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(str(error) if isinstance(error, DeploymentError) else "Deployment failed; inspect the host privately", file=sys.stderr)
        raise SystemExit(1)
