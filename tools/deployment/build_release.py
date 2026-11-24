"""Build and smoke-test the release image; deployment publishes it to the host registry.

The Actions cache keeps unchanged layers byte-identical between builds, so the
host registry receives only layers that changed (usually the application code).
"""

import json
import os
import subprocess
from collections.abc import Mapping
from pathlib import Path

BAKED_PREFIXES = ("HUB_", "DEPLOY_", "LOGFIRE_", "OTEL_", "GITHUB_TOKEN=", "GH_TOKEN=")


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def containerd_store() -> bool:
    """The default builder exports remote caches only with the containerd image store."""
    result = subprocess.run(["docker", "info", "--format", "{{json .DriverStatus}}"], capture_output=True, text=True)
    return result.returncode == 0 and "io.containerd.snapshotter" in result.stdout


def cache_arguments(environment: Mapping[str, str], *, supported: bool = True) -> list[str]:
    """Use the Actions cache when the runtime token is exposed to steps."""
    if not environment.get("ACTIONS_RUNTIME_TOKEN") or not (environment.get("ACTIONS_RESULTS_URL") or environment.get("ACTIONS_CACHE_URL")):
        return []
    if not supported:
        print("::warning::Docker lacks the containerd image store; building without the layer cache")
        return []
    return ["--cache-from", "type=gha,scope=release", "--cache-to", "type=gha,scope=release,mode=max"]


def main():
    revision = os.environ["GITHUB_SHA"]
    image = "msu-hub-bot:" + revision
    run(
        "docker",
        "buildx",
        "build",
        "--platform",
        "linux/amd64",
        "--provenance=false",
        "--sbom=false",
        "--load",
        *cache_arguments(os.environ, supported=containerd_store()),
        "--label",
        "org.opencontainers.image.revision=" + revision,
        "--tag",
        image,
        ".",
    )
    with Path("tools/deployment/image_smoke.py").open() as source:
        run(
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--tmpfs",
            "/tmp:mode=1777",
            "--tmpfs",
            "/work:mode=1777",
            "--mount",
            "type=volume,target=/data",
            "--entrypoint",
            "python",
            "-i",
            image,
            "-",
            stdin=source,
        )
    metadata = json.loads(run("docker", "image", "inspect", image, capture_output=True, text=True).stdout)[0]
    config = metadata.get("Config") or {}
    if any(value.startswith(BAKED_PREFIXES) for value in config.get("Env") or []):
        raise SystemExit("Image contains runtime configuration")
    if (config.get("Labels") or {}).get("org.opencontainers.image.revision") != revision:
        raise SystemExit("Image source revision mismatch")
    if output := os.environ.get("GITHUB_OUTPUT"):
        with Path(output).open("a") as stream:
            stream.write("image=" + image + "\n")
    print("Validated release image " + image)


if __name__ == "__main__":
    main()
