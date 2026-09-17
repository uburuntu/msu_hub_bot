"""Build and test a private image archive for direct SSH delivery."""

import gzip
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def main():
    revision = os.environ["GITHUB_SHA"]
    image = "msu-hub-bot:" + revision
    run(
        "docker",
        "build",
        "--platform",
        "linux/amd64",
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
            "--entrypoint",
            "python",
            "-i",
            image,
            "-",
            stdin=source,
        )
    metadata = json.loads(run("docker", "image", "inspect", image, capture_output=True, text=True).stdout)[0]
    if any(value.startswith(("HUB_", "DEPLOY_", "LOGFIRE_", "OTEL_", "GITHUB_TOKEN=", "GH_TOKEN=")) for value in metadata["Config"]["Env"]):
        raise SystemExit("Image contains runtime configuration")
    directory = Path(tempfile.mkdtemp(prefix="msu-hub-release-", dir=os.environ["RUNNER_TEMP"]))
    archive = directory / "image.tar.gz"
    os.umask(0o077)
    with archive.open("wb") as destination:
        with gzip.GzipFile(filename="", mode="wb", fileobj=destination, compresslevel=1, mtime=0) as compressed:
            process = subprocess.Popen(["docker", "image", "save", image], stdout=subprocess.PIPE)
            try:
                shutil.copyfileobj(process.stdout, compressed, length=1024 * 1024)
            finally:
                process.stdout.close()
            if process.wait():
                raise SystemExit("Image export failed")
    checksum = hashlib.sha256()
    with archive.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            checksum.update(chunk)
    result = {
        "image": metadata["Id"],
        "revision": revision,
        "archive_sha256": checksum.hexdigest(),
        "archive_size": archive.stat().st_size,
    }
    spec = importlib.util.spec_from_file_location("release_validator", Path(__file__).with_name("host.py"))
    validator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validator)
    validator.validate_archive(archive, result)
    (directory / "metadata.json").write_text(json.dumps(result))
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
        output.write("archive=" + str(archive) + "\n")
    print("Validated private image archive: " + metadata["Id"])


if __name__ == "__main__":
    main()
