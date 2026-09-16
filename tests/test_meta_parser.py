"""User-facing grammar contracts shared by message and caption routing."""

import pytest

from common.tg.command import CommandParser, ParsedCommand


@pytest.mark.parametrize("text", [None, "", "  \n\t", "hello", "/another hi", "#another"])
def test_unrelated_or_empty_input(text):
    assert CommandParser("test").parse(text) is None


def test_slash_mention_case_and_remaining_body():
    parser = CommandParser("translate", "tr", args=2)
    assert parser.parse("  /Tr@FriendlyBot ru en  привет\nмир ", username="friendlybot") == ParsedCommand(
        command="Tr", arguments=("ru", "en"), text="привет\nмир"
    )
    assert parser.parse("/tr@OtherBot ru en привет", username="friendlybot") is None


def test_unbounded_arguments_also_remain_in_body():
    assert CommandParser("test").parse("/test one   two\nthree") == ParsedCommand(
        command="test", arguments=("one", "two", "three"), text="one   two\nthree"
    )


def test_missing_positional_arguments_do_not_reject_route():
    assert CommandParser("tts", args=1).parse("/tts") == ParsedCommand(command="tts")


def test_hashtag_arguments_and_surrounding_text():
    assert CommandParser("tr", args=2).parse("before #TR_ru__en_extra after") == ParsedCommand(
        hashtag="TR", arguments=("ru", "en"), text="before  after"
    )


def test_route_matches_its_hashtag_even_when_another_appears_first():
    text = "#py #s"
    assert CommandParser("s").parse(text) == ParsedCommand(hashtag="s", text="#py")
    assert CommandParser("py").parse(text) == ParsedCommand(hashtag="py", text="#s")


def test_first_matching_hashtag_is_removed_only_once():
    assert CommandParser("s").parse("#s first #s second") == ParsedCommand(hashtag="s", text="first #s second")


def test_hashtag_is_still_eligible_after_unmatched_slash():
    assert CommandParser("s").parse("/other@OtherBot #s") == ParsedCommand(hashtag="s", text="/other@OtherBot")


def test_underscore_keyword_and_ordinary_compiler_are_both_matches():
    # Registration order must put this documented stdin route before /py.
    assert CommandParser("py_stdin").parse("#py_stdin code") == ParsedCommand(hashtag="py_stdin", text="code")
    assert CommandParser("py").parse("#py_stdin code") == ParsedCommand(hashtag="py", arguments=("stdin",), text="code")
