"""Exercise the pure command grammar against captured, expanded route policies."""

import json
from pathlib import Path

import pytest

from common.tg.command import CommandParser, ParsedCommand

CONTRACT = json.loads((Path(__file__).parent / "fixtures/routing_contract.json").read_text())
MESSAGE_ROUTES = [route for route in CONTRACT["routes"] if route["event"] == "message"]
META_ROUTES = [(route, spec) for route in MESSAGE_ROUTES for spec in route["filters"] if spec["type"] == "MetaCommand"]
META_CASES = [case for case in CONTRACT["cases"] if "meta" in case]


@pytest.mark.parametrize("route,spec", META_ROUTES, ids=[route["key"] for route, _ in META_ROUTES])
def test_every_retained_meta_alias_uses_the_same_grammar(route, spec):
    parser = CommandParser(*route["aliases"], args=spec["args"])
    for alias in route["aliases"]:
        result = parser.parse(f"/{alias} synthetic input")
        assert result is not None
        assert result.command == alias
        assert result.hashtag == ""
        assert parser.parse(f"/{alias.upper()}@CONTRACT_BOT input", username="contract_bot") is not None
        assert parser.parse(f"/{alias}@other_bot input", username="contract_bot") is None


@pytest.mark.parametrize("case", META_CASES, ids=[case["id"] for case in META_CASES])
def test_parser_preserves_captured_arguments_and_remaining_body(case):
    route = MESSAGE_ROUTES[case["matched"]["event_order"]]
    spec = next(spec for spec in route["filters"] if spec["type"] == "MetaCommand")
    parser = CommandParser(*route["aliases"], args=spec["args"])
    expected = ParsedCommand(**{**case["meta"], "arguments": tuple(case["meta"]["arguments"])})
    assert parser.parse(case.get("text") or case.get("caption"), username="contract_bot") == expected


@pytest.mark.parametrize("case", CONTRACT["cases"][:10], ids=[case["id"] for case in CONTRACT["cases"][:10]])
def test_cross_feature_parser_matches_retain_the_captured_precedence(case):
    # This freezes the observed order, including the explicitly marked stdin defect.
    # The migrated dispatcher applies approved_target for those two cases.
    for route, spec in META_ROUTES:
        parser = CommandParser(*route["aliases"], args=spec["args"])
        if parser.parse(case["text"], username="contract_bot") is not None:
            assert route["key"] == case["matched"]["route_key"]
            break
    else:
        pytest.fail("Captured command no longer matches any retained route")


def test_message_and_edited_compiler_aliases_remain_symmetric():
    message = {
        (route["handler"], route["generated"]["language"], tuple(route["aliases"]))
        for route in CONTRACT["routes"]
        if route["event"] == "message" and "language" in route.get("generated", {})
    }
    edited = {
        (route["handler"], route["generated"]["language"], tuple(route["aliases"]))
        for route in CONTRACT["routes"]
        if route["event"] == "edited_message" and "language" in route.get("generated", {})
    }
    assert message == edited
    assert len(message) == 146
