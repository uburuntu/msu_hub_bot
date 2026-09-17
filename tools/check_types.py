"""Keep explicitly checked modules strict and prevent accidental coverage loss."""

import argparse
import fnmatch
import os
import subprocess
import tomllib
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import cast

ROOT = Path(__file__).resolve().parents[1]
APPLICATION_ROOTS = ("msu_hub_bot", "common", "hub_bot", "src/msu_hub_bot")
# Third-party source is verified by test_api2ch_compat.py and has a narrow API stub.
VENDORED_PREFIXES = ("common/externals/_api2ch/", "src/msu_hub_bot/providers/_api2ch/")


def is_application(path: str) -> bool:
    return (
        path.endswith(".py") and any(path.startswith(f"{root}/") for root in APPLICATION_ROOTS) and not path.startswith(VENDORED_PREFIXES)
    )


def changed_paths(diff: str) -> dict[str, str | None]:
    """Read Git's NUL-delimited name-status format without guessing file identity."""
    fields = iter(diff.rstrip("\0").split("\0") if diff else [])
    changes: dict[str, str | None] = {}
    for status in fields:
        try:
            source = next(fields)
            if status.startswith(("R", "C")):
                destination = next(fields)
                if status.startswith("R"):
                    changes[source] = destination
            elif status == "D":
                changes[source] = None
        except StopIteration as error:
            raise ValueError("Incomplete Git name-status output") from error
    return changes


def table(value: object) -> Mapping[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError("Expected a configuration table")
    return cast(Mapping[str, object], value)


def mypy_options(document: str) -> Mapping[str, object]:
    return table(table(tomllib.loads(document).get("tool", {})).get("mypy", {}))


def checked_files(options: Mapping[str, object]) -> frozenset[str]:
    raw = options.get("files", [])
    if not isinstance(raw, list):
        raise ValueError("mypy.files must be an explicit list of Python file paths")
    paths: set[str] = set()
    for value in raw:
        if not isinstance(value, str):
            raise ValueError("mypy.files entries must be strings")
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or path.suffix != ".py" or any(char in value for char in "*?["):
            raise ValueError("mypy.files must contain relative Python paths without patterns")
        if str(path) != value or value in paths:
            raise ValueError("mypy.files must contain unique canonical paths")
        paths.add(value)
    return frozenset(paths)


def policy_errors(options: Mapping[str, object], scope: frozenset[str]) -> list[str]:
    errors = []
    if not scope:
        errors.append("The checked-module list is empty")
    for key in ("strict", "warn_unused_configs", "warn_unused_ignores", "show_error_codes"):
        if options.get(key) is not True:
            errors.append(f"mypy.{key} must remain enabled")
    for key in ("ignore_errors", "ignore_missing_imports"):
        if options.get(key):
            errors.append(f"Global mypy.{key} hides unchecked code")
    if options.get("follow_imports", "normal") != "normal" or options.get("disable_error_code"):
        errors.append("Global import or error suppression is not allowed")
    strict_flags = ("check_untyped_defs", "warn_return_any", "strict_optional", "warn_unused_ignores")
    if any(value is False for key, value in options.items() if key.startswith("disallow_") or key in strict_flags):
        errors.append("Do not disable strict checks globally")

    raw_overrides = options.get("overrides", [])
    if not isinstance(raw_overrides, list):
        return [*errors, "mypy.overrides must be a list"]
    modules = {path.removeprefix("src/").removesuffix(".py").replace("/", ".").removesuffix(".__init__") for path in scope}
    for raw in raw_overrides:
        override = table(raw)
        selectors = override.get("module", [])
        if isinstance(selectors, str):
            selectors = [selectors]
        if not isinstance(selectors, list) or not selectors or not all(isinstance(item, str) for item in selectors):
            errors.append("Each mypy override must name specific modules")
            continue
        for selector in selectors:
            if selector == "*" or selector in {f"{root.rsplit('/', 1)[-1]}.*" for root in APPLICATION_ROOTS}:
                errors.append(f"Broad application override is not allowed: {selector}")
            if any(fnmatch.fnmatchcase(module, selector) for module in modules):
                errors.append(f"Checked modules must use the strict global policy: {selector}")
    return errors


def coverage_errors(
    scope: frozenset[str],
    previous: frozenset[str],
    existing: set[str],
    added: set[str],
    renames: Mapping[str, str | None] | None = None,
) -> list[str]:
    identities = renames or {}
    retained = {destination for path in previous if (destination := identities.get(path, path)) is not None}
    errors = [f"Checked module left the scope: {path}" for path in sorted((retained & existing) - scope)]
    errors.extend(f"New application module is outside the scope: {path}" for path in sorted(added - scope))
    errors.extend(f"Checked module does not exist: {path}" for path in sorted(scope - existing))
    return errors


def git(*arguments: str) -> str:
    return subprocess.run(["git", *arguments], cwd=ROOT, check=True, capture_output=True, text=True).stdout


def file_identities(base: str, paths: set[str]) -> dict[str, str | None]:
    """Follow actual commit edges so formatting before a move cannot erase its identity."""
    # Prefer the longest ancestry path through merges, including CI's synthetic PR
    # merge; a direct base-to-merge diff can hide the intervening rename commit.
    ancestry: dict[str, list[str]] = {base: []}
    for line in git("rev-list", "--ancestry-path", "--reverse", "--topo-order", "--parents", f"{base}..HEAD").splitlines():
        revision, *parents = line.split()
        candidates = [ancestry[parent] for parent in parents if parent in ancestry]
        if candidates:
            ancestry[revision] = [*max(candidates, key=len), revision]
    head = git("rev-parse", "HEAD").strip()
    revisions = ancestry.get(head, [head])
    identities: dict[str, str | None] = {path: path for path in paths}
    previous = base
    for revision in [*revisions, ""]:
        target = [revision] if revision else []
        changes = changed_paths(git("diff", "--name-status", "-z", "--find-renames", previous, *target, "--", "*.py"))
        identities = {original: changes.get(current, current) if current is not None else None for original, current in identities.items()}
        previous = revision
    return identities


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-ref", default=os.environ.get("TYPECHECK_BASE_REF") or "HEAD^")
    arguments = parser.parse_args()
    try:
        base = git("rev-parse", "--verify", "--end-of-options", f"{arguments.base_ref}^{{commit}}").strip()
        options = mypy_options((ROOT / "pyproject.toml").read_text())
        scope = checked_files(options)
        previous = checked_files(mypy_options(git("show", f"{base}:pyproject.toml")))
        tracked = set(filter(None, git("ls-files", "--cached", "--others", "--exclude-standard", "-z").split("\0")))
        existing = {path for path in tracked | scope if (ROOT / path).is_file()}
        baseline = {
            path for path in git("ls-tree", "-r", "--name-only", "-z", base, "--", *APPLICATION_ROOTS).split("\0") if is_application(path)
        }
        identities = file_identities(base, baseline | previous)
        application = {path for path in existing if is_application(path)}
        retained = {destination for source, destination in identities.items() if source in baseline and destination is not None}
        added = application - retained
        errors = policy_errors(options, scope) + coverage_errors(scope, previous, existing, added, identities)
        if added - scope and baseline - existing:
            untracked = set(git("ls-files", "--others", "--exclude-standard", "-z").split("\0"))
            if (added - scope) & untracked:
                errors.append("Stage renamed files before checking type scope so Git can identify their original paths")
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"Type scope check could not complete: {type(error).__name__}")
        return 1
    if errors:
        print("\n".join(errors))
        return 1
    print(f"Strict mypy scope checked: {len(scope)} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
