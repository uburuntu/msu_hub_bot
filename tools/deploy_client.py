"""Send a deployment request over verified SSH without logging configuration."""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path

PUBLIC_STATUS = {
    "Image received; checking configuration and connections",
    "Preflight passed; stopping the current poller",
    "Release failed; restoring the previous poller",
    "Previous poller restored",
    "Rollback completed",
    "Rollback failed; restoring the current release",
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
        if line in PUBLIC_STATUS or re.fullmatch(r"Deployed [0-9a-f]{40} sha256:[0-9a-f]{64}", line):
            print(line)
    if result.returncode:
        print("Deployment failed; inspect the host privately for details", file=sys.stderr)


def main():
    host = os.environ["DEPLOY_HOST"]
    user = os.environ["DEPLOY_USER"]
    port = int(os.environ.get("DEPLOY_PORT", "22"))
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.:-]*", host) or not re.fullmatch(r"[a-z_][a-z0-9_-]*", user) or not 1 <= port <= 65535:
        raise SystemExit("Invalid SSH connection settings")
    operation = os.environ.get("DEPLOY_OPERATION", "deploy")
    payload = {"action": operation}
    archive = None
    if operation == "deploy":
        archive = Path(os.environ["DEPLOY_ARCHIVE"])
        metadata = json.loads(archive.with_name("metadata.json").read_text())
        if metadata["revision"] != os.environ["GITHUB_SHA"]:
            raise SystemExit("Image archive does not match the deployment revision")
        payload.update(
            metadata,
            environment=runtime_environment(os.environ),
        )
    with tempfile.TemporaryDirectory(prefix="msu-hub-ssh-") as directory:
        directory = Path(directory)
        key = directory / "key"
        key.write_text(os.environ["DEPLOY_SSH_KEY"].strip() + "\n")
        key.chmod(0o600)
        known_hosts = directory / "known_hosts"
        known_hosts.write_text(os.environ["DEPLOY_KNOWN_HOSTS"].strip() + "\n")
        command = [
            "ssh",
            "-T",
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
            user + "@" + host,
            "msu-hub-bot",
        ]
        print("Connecting to deployment host", flush=True)
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=stdout, stderr=stderr)
            try:
                process.stdin.write(json.dumps(payload).encode() + b"\n")
                if archive:
                    with archive.open("rb") as source:
                        shutil.copyfileobj(source, process.stdin, length=1024 * 1024)
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
