from pathlib import Path

import pytest

from tools.check_types import checked_files, coverage_errors, mypy_options, policy_errors

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
