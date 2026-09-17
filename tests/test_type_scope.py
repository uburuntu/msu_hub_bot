import subprocess
import sys
from pathlib import Path

import pytest

from tools import check_types
from tools.check_types import changed_paths, checked_files, coverage_errors, is_application, mypy_options, policy_errors

ROOT = Path(__file__).resolve().parents[1]


def test_current_type_policy_is_strict():
    options = mypy_options((ROOT / "pyproject.toml").read_text())
    assert not policy_errors(options, checked_files(options))


def test_scope_cannot_drop_existing_modules_or_omit_new_application_code():
    previous = frozenset({"common/old.py", "common/deleted.py"})
    scope = frozenset({"common/new.py"})
    errors = coverage_errors(scope, previous, {"common/old.py", "common/new.py", "hub_bot/new.py"}, {"hub_bot/new.py"})
    assert errors == [
        "Checked module left the scope: common/old.py",
        "New application module is outside the scope: hub_bot/new.py",
    ]


def test_scope_allows_a_typed_module_rename_and_rejects_nonexistent_files():
    assert not coverage_errors(frozenset({"common/new.py"}), frozenset({"common/old.py"}), {"common/new.py"}, {"common/new.py"})
    assert coverage_errors(frozenset({"common/missing.py"}), frozenset(), set(), set()) == [
        "Checked module does not exist: common/missing.py"
    ]


def test_scope_keeps_a_renamed_checked_module_strict():
    old = "common/checked.py"
    new = "src/msu_hub_bot/checked.py"
    assert coverage_errors(frozenset(), frozenset({old}), {new}, set(), {old: new}) == [f"Checked module left the scope: {new}"]
    assert not coverage_errors(frozenset({new}), frozenset({old}), {new}, set(), {old: new})


def test_git_rename_format_preserves_spaces_and_does_not_treat_copies_as_moves():
    assert changed_paths("M\0same.py\0R090\0old file.py\0new file.py\0C100\0source.py\0copy.py\0D\0removed.py\0") == {
        "old file.py": "new file.py",
        "removed.py": None,
    }
    with pytest.raises(ValueError, match="Incomplete"):
        changed_paths("R100\0old.py\0")


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("common/old.py", True),
        ("hub_bot/commands/old.py", True),
        ("msu_hub_bot/settings.py", True),
        ("src/msu_hub_bot/settings.py", True),
        ("src/msu_hub_bot_extra/new.py", False),
        ("common/externals/_api2ch/api.py", False),
        ("src/msu_hub_bot/providers/_api2ch/api.py", False),
        ("src/msu_hub_bot/providers/dvach.py", True),
    ],
)
def test_application_paths_and_narrow_vendor_exclusion(path, expected):
    assert is_application(path) is expected


@pytest.mark.parametrize("paths", [["common/*.py"], ["../elsewhere.py"], ["/tmp/module.py"], ["common/a.py", "common/a.py"], "common/a.py"])
def test_scope_rejects_ambiguous_file_lists(paths):
    with pytest.raises(ValueError):
        checked_files({"files": paths})


@pytest.mark.parametrize(
    "weakening",
    [
        {"strict": False},
        {"ignore_errors": True},
        {"ignore_missing_imports": True},
        {"follow_imports": "skip"},
        {"disable_error_code": ["arg-type"]},
        {"disallow_untyped_defs": False},
        {"overrides": [{"module": "common.*", "ignore_errors": True}]},
        {"overrides": [{"module": "common.checked", "follow_imports": "skip"}]},
    ],
)
def test_scope_rejects_global_and_checked_module_suppressions(weakening):
    options = {"strict": True, "warn_unused_configs": True, "warn_unused_ignores": True, "show_error_codes": True, **weakening}
    assert policy_errors(options, frozenset({"common/checked.py"}))


def test_scope_allows_a_narrow_untyped_dependency_boundary():
    options = {
        "strict": True,
        "warn_unused_configs": True,
        "warn_unused_ignores": True,
        "show_error_codes": True,
        "overrides": [{"module": "external_sdk", "ignore_missing_imports": True}],
    }
    assert not policy_errors(options, frozenset({"common/checked.py"}))


@pytest.mark.parametrize("selector", ["msu_hub_bot.*", "msu_hub_bot.checked"])
def test_src_layout_does_not_hide_checked_modules_from_override_policy(selector):
    options = {
        "strict": True,
        "warn_unused_configs": True,
        "warn_unused_ignores": True,
        "show_error_codes": True,
        "overrides": [{"module": selector, "follow_imports": "skip"}],
    }
    assert policy_errors(options, frozenset({"src/msu_hub_bot/checked.py"}))


def run_git(repo, *arguments):
    return subprocess.run(
        ["git", "-c", "user.name=Type Scope Test", "-c", "user.email=types@example.invalid", *arguments],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def write_scope(repo, *paths):
    entries = ", ".join(f'"{path}"' for path in paths)
    (repo / "pyproject.toml").write_text(
        "[tool.mypy]\nstrict = true\nwarn_unused_configs = true\nwarn_unused_ignores = true\nshow_error_codes = true\n"
        f"files = [{entries}]\n"
    )


@pytest.fixture
def type_repo(tmp_path, monkeypatch):
    run_git(tmp_path, "init", "-q")
    (tmp_path / "common").mkdir()
    for name in ("checked", "legacy", "stable"):
        (tmp_path / "common" / f"{name}.py").write_text(
            f"def {name}():\n" + "".join(f"    # {name} item {number}\n" for number in range(20)) + "    return 1\n"
        )
    write_scope(tmp_path, "common/checked.py", "common/stable.py")
    run_git(tmp_path, "add", ".")
    run_git(tmp_path, "commit", "-qm", "Baseline")
    monkeypatch.setattr(check_types, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["check_types.py", "--base-ref", "HEAD"])
    return tmp_path


def relocate_modules(repo):
    destination = repo / "src/msu_hub_bot"
    destination.mkdir(parents=True)
    for name in ("checked", "legacy"):
        (repo / "common" / f"{name}.py").rename(destination / f"{name}.py")
    write_scope(repo, "src/msu_hub_bot/checked.py", "common/stable.py")


@pytest.mark.parametrize("commit_move", [False, True])
def test_git_scope_accepts_staged_and_committed_moves_with_worktree_edits(type_repo, monkeypatch, capsys, commit_move):
    baseline = run_git(type_repo, "rev-parse", "HEAD").strip()
    relocate_modules(type_repo)
    run_git(type_repo, "add", "-A")
    if commit_move:
        run_git(type_repo, "commit", "-qm", "Relocate")
    checked = type_repo / "src/msu_hub_bot/checked.py"
    checked.write_text(checked.read_text().replace("return 1", "return 2"))
    monkeypatch.setattr(sys, "argv", ["check_types.py", "--base-ref", baseline])
    assert check_types.main() == 0
    assert "Strict mypy scope checked: 2 files" in capsys.readouterr().out


def test_git_scope_does_not_lose_coverage_during_a_rename(type_repo, capsys):
    relocate_modules(type_repo)
    write_scope(type_repo, "common/stable.py")
    run_git(type_repo, "add", "-A")
    assert check_types.main() == 1
    assert "Checked module left the scope: src/msu_hub_bot/checked.py" in capsys.readouterr().out


def test_unstaged_move_has_actionable_error_without_changing_the_index(type_repo, capsys):
    relocate_modules(type_repo)
    index_before = run_git(type_repo, "ls-files", "--stage", "-z")
    assert check_types.main() == 1
    assert "Stage renamed files" in capsys.readouterr().out
    assert run_git(type_repo, "ls-files", "--stage", "-z") == index_before


@pytest.mark.parametrize("stage", [False, True])
def test_git_scope_requires_new_application_modules_to_be_checked(type_repo, capsys, stage):
    (type_repo / "common/new.py").write_text("def new():\n    return 'new'\n")
    if stage:
        run_git(type_repo, "add", "common/new.py")
    assert check_types.main() == 1
    assert "New application module is outside the scope: common/new.py" in capsys.readouterr().out


@pytest.mark.parametrize("merge", [False, True])
def test_separate_edits_and_renames_retain_identity_through_commit_history(type_repo, monkeypatch, capsys, merge):
    baseline = run_git(type_repo, "rev-parse", "HEAD").strip()
    run_git(type_repo, "checkout", "-qb", "relocate")
    legacy = type_repo / "common/legacy.py"
    legacy.write_text("def rewritten():\n    return 'different lines before relocation'\n")
    run_git(type_repo, "add", "-A")
    run_git(type_repo, "commit", "-qm", "Edit before moving")
    relocate_modules(type_repo)
    run_git(type_repo, "add", "-A")
    run_git(type_repo, "commit", "-qm", "Move")
    if merge:
        run_git(type_repo, "checkout", "-qb", "integration", baseline)
        run_git(type_repo, "merge", "--no-ff", "-m", "PR merge", "relocate")
    monkeypatch.setattr(sys, "argv", ["check_types.py", "--base-ref", baseline])
    assert check_types.main() == 0
    assert "Strict mypy scope checked: 2 files" in capsys.readouterr().out


def test_deleted_and_recreated_unchecked_path_is_new_code(type_repo, monkeypatch, capsys):
    baseline = run_git(type_repo, "rev-parse", "HEAD").strip()
    legacy = type_repo / "common/legacy.py"
    legacy.unlink()
    run_git(type_repo, "add", "-A")
    run_git(type_repo, "commit", "-qm", "Remove unused module")
    legacy.write_text("def replacement():\n    return 'new implementation'\n")
    run_git(type_repo, "add", "-A")
    monkeypatch.setattr(sys, "argv", ["check_types.py", "--base-ref", baseline])
    assert check_types.main() == 1
    assert "New application module is outside the scope: common/legacy.py" in capsys.readouterr().out
