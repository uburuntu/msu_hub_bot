"""Uploader identifiers retain JavaScript base-32 formatting without a JS runtime."""

import pytest

from msu_hub_bot.providers import topdf


@pytest.mark.parametrize(
    ("milliseconds", "random_values", "expected"),
    [
        (0, [0.0] * 5, "o_0000000"),
        (1, [0.5] * 5, "o_1vvvvvvvvvvvvvvv0"),
        (32, [0.0, 1 / 65535, 31 / 65535, 32 / 65535, 32768 / 65535], "o_1001v1010000"),
        (1700000123456, [0.1, 0.25, 0.5, 0.75, 0.9999999999999999], "o_1hf7ueii06cpfvvvvv1fvv1vvu0"),
    ],
)
def test_id_matches_javascript_vectors(
    milliseconds: int, random_values: list[float], expected: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Vectors come from Number.toString(32) and Math.floor(65535 * value).
    values = iter(random_values)
    monkeypatch.setattr(topdf.time, "time", lambda: milliseconds / 1000)
    monkeypatch.setattr(topdf.random, "random", lambda: next(values))

    assert topdf._conversion_id() == expected
    assert list(values) == []


def test_id_truncates_submillisecond_time_and_resets_counter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(topdf.time, "time", lambda: 0.032999)
    monkeypatch.setattr(topdf.random, "random", lambda: 0.0)

    assert topdf._conversion_id() == "o_10000000"
    assert topdf._conversion_id() == "o_10000000"
