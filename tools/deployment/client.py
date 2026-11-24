"""Publish a release through the host registry and request deployment over verified SSH.

Configuration is never logged. Only layers the host registry lacks cross the
network: unchanged dependency layers keep their digests through the build cache.
"""

import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from pathlib import Path

REMOTE_REGISTRY = "127.0.0.1:5000"
REPOSITORY = "msu-hub-bot"
MANIFEST_TYPES = (
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.docker.distribution.manifest.v2+json",
)
INDEX_TYPES = {MANIFEST_TYPES[0], MANIFEST_TYPES[2]}

PUBLIC_STATUS = {
    "Image received; checking configuration and connections",
    "Preflight passed; stopping the current poller",
    "Release failed; restoring the previous poller",
    "Previous poller restored",
    "Rollback completed",
    "Rollback failed; restoring the current release",
    "Registry exceeds its expected size; inspect it privately",
    "Insufficient disk space for the release; free space on the host",
}


def runtime_environment(environment: Mapping[str, str]) -> dict[str, str]:
    """The project write token is the only non-HUB runtime credential."""
    export_enabled = environment.get("HUB_TELEMETRY_ENABLED", "").casefold() in {"1", "true", "yes"}
    return {
        key: value
        for key, value in environment.items()
        if (re.fullmatch(r"HUB_[A-Z0-9_]+", key) or (export_enabled and key == "LOGFIRE_TOKEN")) and key != "HUB_CONFIG_JSON" and value
    }


def report_result(result):
    # SSH diagnostics can expose resolved IPs or host paths in public Actions logs.
    for line in result.stdout.splitlines():
        if line in PUBLIC_STATUS or re.fullmatch(r"Deployed [0-9a-f]{40} sha256:[0-9a-f]{64}|Registry holds [0-9]+ MB", line):
            print(line)
    if result.returncode:
        print("Deployment failed; inspect the host privately for details", file=sys.stderr)


class PublishError(Exception):
    pass


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def fetch_manifest(registry: str, reference: str) -> tuple[str, dict]:
    request = urllib.request.Request(
        f"http://{registry}/v2/{REPOSITORY}/manifests/{reference}", headers={"Accept": ", ".join(MANIFEST_TYPES)}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read(4 * 1024 * 1024 + 1)
    if len(body) > 4 * 1024 * 1024:
        raise PublishError("Oversized manifest")
    return "sha256:" + hashlib.sha256(body).hexdigest(), json.loads(body)


def resolve_release(registry: str, revision: str) -> tuple[str, str]:
    """Return the linux/amd64 manifest digest and its configuration digest."""
    digest, document = fetch_manifest(registry, revision)
    if document.get("mediaType") in INDEX_TYPES or "manifests" in document:
        platforms = [
            item
            for item in document.get("manifests", [])
            if (item.get("platform") or {}).get("os") == "linux" and (item.get("platform") or {}).get("architecture") == "amd64"
        ]
        if len(platforms) != 1:
            raise PublishError("Release must contain exactly one linux/amd64 image")
        digest, document = fetch_manifest(registry, platforms[0]["digest"])
    config = (document.get("config") or {}).get("digest")
    if not isinstance(config, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", config):
        raise PublishError("Invalid release manifest")
    return digest, config


def wait_for_registry(registry: str, tunnel: subprocess.Popen, deadline: float = 30) -> None:
    until = time.monotonic() + deadline
    while time.monotonic() < until:
        if tunnel.poll() is not None:
            raise PublishError("Registry tunnel closed")
        try:
            with urllib.request.urlopen(f"http://{registry}/v2/", timeout=5):
                return
        except (OSError, urllib.error.URLError):
            time.sleep(0.5)
    raise PublishError("Registry tunnel did not open")


def push_tag(revision: str, environment: Mapping[str, str]) -> str:
    """A tag per push attempt keeps every pushed manifest visible to the host's sweep."""
    run, attempt = environment.get("GITHUB_RUN_ID", "0"), environment.get("GITHUB_RUN_ATTEMPT", "1")
    if not run.isdigit() or not attempt.isdigit():
        raise PublishError("Invalid workflow run identity")
    return f"{revision}-{run}-{attempt}"


def publish(ssh: list[str], destination: str, image: str, revision: str) -> tuple[str, str]:
    """Push through a forward limited to the host registry, then resolve its digests."""
    tag = push_tag(revision, os.environ)
    port = free_port()
    registry = f"127.0.0.1:{port}"
    tunnel = subprocess.Popen(
        [*ssh, "-N", "-o", "ExitOnForwardFailure=yes", "-L", f"{registry}:{REMOTE_REGISTRY}", destination],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    remote = f"{registry}/{REPOSITORY}:{tag}"
    try:
        wait_for_registry(registry, tunnel)
        subprocess.run(["docker", "image", "tag", image, remote], check=True, capture_output=True)
        started = time.monotonic()
        if subprocess.run(["docker", "image", "push", "--quiet", remote], capture_output=True).returncode:
            raise PublishError("Image push failed")
        print(f"Image published in {time.monotonic() - started:.0f}s", flush=True)
        return resolve_release(registry, tag)
    finally:
        subprocess.run(["docker", "image", "rm", remote], capture_output=True)
        tunnel.terminate()
        try:
            tunnel.wait(10)
        except subprocess.TimeoutExpired:
            tunnel.kill()


def main():
    host = os.environ["DEPLOY_HOST"]
    user = os.environ["DEPLOY_USER"]
    port = int(os.environ.get("DEPLOY_PORT", "22"))
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.:-]*", host) or not re.fullmatch(r"[a-z_][a-z0-9_-]*", user) or not 1 <= port <= 65535:
        raise SystemExit("Invalid SSH connection settings")
    operation = os.environ.get("DEPLOY_OPERATION", "deploy")
    payload: dict[str, object] = {"action": operation}
    revision = os.environ.get("GITHUB_SHA", "")
    image = os.environ.get("DEPLOY_IMAGE", "")
    if operation == "deploy" and (not re.fullmatch(r"[0-9a-f]{40}", revision) or image != "msu-hub-bot:" + revision):
        raise SystemExit("Release image does not match the deployment revision")
    with tempfile.TemporaryDirectory(prefix="msu-hub-ssh-") as directory:
        directory = Path(directory)
        key = directory / "key"
        key.write_text(os.environ["DEPLOY_SSH_KEY"].strip() + "\n")
        key.chmod(0o600)
        known_hosts = directory / "known_hosts"
        known_hosts.write_text(os.environ["DEPLOY_KNOWN_HOSTS"].strip() + "\n")
        ssh = [
            "ssh",
            "-i",
            str(key),
            "-p",
            str(port),
            "-o",
            "BatchMode=yes",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "UserKnownHostsFile=" + str(known_hosts),
            "-o",
            "ConnectTimeout=20",
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "ServerAliveCountMax=3",
        ]
        destination = user + "@" + host
        if operation == "deploy":
            try:
                manifest, config = publish(ssh, destination, image, revision)
            except (PublishError, OSError, ValueError, subprocess.CalledProcessError):
                print("Image publication failed; inspect the host registry privately", file=sys.stderr)
                raise SystemExit(1)
            payload.update(image=config, revision=revision, manifest=manifest, environment=runtime_environment(os.environ))
        command = [*ssh, "-T", destination, "msu-hub-bot"]
        print("Connecting to deployment host", flush=True)
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=stdout, stderr=stderr)
            try:
                process.stdin.write(json.dumps(payload).encode() + b"\n")
                process.stdin.close()
            except BrokenPipeError:
                pass
            returncode = process.wait()
            stdout.seek(0)
            result = subprocess.CompletedProcess(command, returncode, stdout.read().decode(errors="replace"))
        report_result(result)
        raise SystemExit(result.returncode)


if __name__ == "__main__":
    try:
        main()
    except (KeyError, ValueError, OSError):
        print("Deployment connection configuration is incomplete or invalid", file=sys.stderr)
        raise SystemExit(1)
