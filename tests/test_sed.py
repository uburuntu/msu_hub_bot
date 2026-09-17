import asyncio
import time

import pytest

from msu_hub_bot.execution.executor import TPExecutor
from msu_hub_bot.execution import sed


@pytest.mark.parametrize(
    "text, commands, expected",
    [
        ("Cat cat", ["s/cat/dog/i"], "dog dog"),
        ("Cat cat", ["s/cat/dog/"], "dog dog"),
        ("a\nb", ["s/^/!/m"], "!a\n!b"),
        ("a/b", [r"s/a\/b/c/"], "c"),
        ("before", [r"s/before/after\/path/"], "after/path"),
        ("ab", [r"s/(a)(b)/\2\1/"], "ba"),
        ("Привет", ["ы/привет/Пока/i"], "Пока"),
        ("aaa", ["s/a/b/", "s/b/c/"], "ccc"),
        ("a", ["s/a//"], ""),
        ("original", ["s/[/bad/"], "original"),
        ("original", ["s/original/bad/L"], "original"),
        ("original", ["not a substitution"], None),
    ],
)
def test_substitution_syntax_and_flags(text, commands, expected):
    assert sed.sed_calc(text, commands) == expected


def test_substitutions_have_bounded_output_and_count():
    assert len(sed.sed_calc("a" * 4096, ["s/a/" + "b" * 2000 + "/"])) == 4096
    assert sed.sed_calc("a", ["s/a/b/"] * 5 + ["s/b/c/"]) == "b"


async def test_pathological_regex_is_killed_and_worker_slot_recovers(monkeypatch):
    monkeypatch.setattr(sed, "SED_TIMEOUT", 0.2)
    executor = TPExecutor(1)
    started = time.monotonic()
    try:
        with pytest.raises(sed.SedTimeout):
            await asyncio.wait_for(executor.run(sed.sed_calc, "a" * 1000 + "!", ["s/(a+)+$/x/"]), 2)
        assert time.monotonic() - started < 2
        monkeypatch.setattr(sed, "SED_TIMEOUT", 2)
        result, timed_out = await executor.run(sed.sed_calc, "hello", ["s/hello/bye/"], timeout=2)
        assert (result, timed_out) == ("bye", False)
    finally:
        executor.shutdown(wait=True)
