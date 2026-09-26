from datetime import UTC, datetime
from typing import Annotated, Literal, LiteralString, NotRequired, Protocol, TypedDict
from unittest.mock import AsyncMock

import pytest
from aiogram.types import Chat, Document, Message, Update, User
from pydantic import Field, TypeAdapter, ValidationError

from teleforge import App, Feature, command
from teleforge.feature import CompilationError
from teleforge.inputs import Argument, ImageInput, TextInput
from teleforge.issues import ConfigurationError
from teleforge.testing import RecordingBot


def incoming(text: str | None = None, **values) -> Update:
    return Update(
        update_id=1,
        message=Message(
            message_id=10,
            date=datetime.now(UTC),
            chat=Chat(id=1, type="private"),
            from_user=User(id=7, is_bot=False, first_name="actor"),
            text=text,
            **values,
        ),
    )


@pytest.mark.parametrize(
    ("annotation", "token", "default", "expected"),
    [
        (Literal[1, 2], "2", 1, "int:2"),
        (Literal["first", "second"], "second", "first", "str:second"),
        (Literal[True], "true", True, "bool:True"),
        (Literal[False], "0", False, "bool:False"),
        (Literal[True, 1], "1", True, "int:1"),
        (Literal[1, True], "true", 1, "bool:True"),
        (Literal["1", 1, True], "1", True, "str:1"),
        (Literal[1] | Literal["2", 2], "2", 1, "str:2"),
    ],
)
async def test_literal_command_tokens_select_exact_typed_members(annotation, token, default, expected):
    class Pick(Feature):
        @command("pick")
        async def pick(self, value: annotation = default) -> str:
            return f"{type(value).__name__}:{value}"

    bot = RecordingBot()
    async with App(Pick()) as app:
        assert app.check() == ()
        await app.feed_update(bot, incoming(f"/pick {token}"))
    assert bot.requests[-1].text == expected


async def test_invalid_literal_is_safe_guidance_without_equality_coercion():
    issues = []

    class Pick(Feature):
        @command("pick", value=Argument(strict=True))
        async def pick(self, value: Literal[1]) -> None:
            pytest.fail("Boolean spelling was accepted as the integer literal")

    def formatter(issue):
        issues.append((issue.code, dict(issue.params)))
        return "Choose 1."

    bot = RecordingBot()
    async with App(Pick(), input_formatter=formatter) as app:
        await app.feed_update(bot, incoming("/pick true"))
    assert issues == [("argument-invalid", {"parameter": "value"})]
    assert bot.requests[-1].text == "Choose 1."


@pytest.mark.parametrize("replied", [False, True])
async def test_text_constraints_are_safe_localizable_acquisition_issues(replied):
    issues = []

    class Copy(Feature):
        @command("copy", text=TextInput())
        async def copy(self, text: Annotated[str, Field(min_length=20)]) -> None:
            pytest.fail("Invalid acquired text reached the handler")

    def formatter(issue):
        issues.append((issue.code, dict(issue.params)))
        return "Send longer text."

    update = (
        incoming("/copy", reply_to_message=incoming("private-short").message)
        if replied
        else incoming("/copy private-short")
    )
    bot = RecordingBot()
    async with App(Copy(), input_formatter=formatter) as app:
        await app.feed_update(bot, update)
    assert issues == [("text-invalid", {"parameter": "text"})]
    assert len(bot.requests) == 1
    assert bot.requests[0].text == "Send longer text."


@pytest.mark.parametrize("case", ["text", "media", "literal", "clamp"])
async def test_invalid_author_defaults_and_clamps_are_configuration_errors(case):
    class Invalid(Feature):
        @command("text", text=TextInput())
        async def text(self, text: Annotated[str, Field(min_length=5)] = "ab") -> None:
            pytest.fail("Invalid default reached the handler")

        @command("media", media=ImageInput())
        async def media(self, media: bytes = 123) -> None:
            pytest.fail("Invalid media default reached the handler")

        @command("literal")
        async def literal(self, value: Literal[1] = True) -> None:
            pytest.fail("Boolean default became an integer literal")

        @command("clamp", value=Argument(clamp=(1, 5)))
        async def clamp(self, value: Annotated[int, Field(ge=10)]) -> None:
            pytest.fail("Incompatible clamp reached the handler")

    bot = RecordingBot()
    async with App(Invalid()) as app:
        with pytest.raises(ConfigurationError, match="invalid default|incompatible clamp"):
            await app.feed_update(bot, incoming(f"/{case}" + (" 20" if case == "clamp" else "")))
    assert not bot.requests


async def test_handler_validation_error_is_not_acquisition_guidance():
    class Domain(Feature):
        @command("domain", text=TextInput())
        async def domain(self, text: str) -> None:
            TypeAdapter(int).validate_python("domain failure")

    bot = RecordingBot()
    async with App(Domain()) as app:
        with pytest.raises(ValidationError):
            await app.feed_update(bot, incoming("/domain valid input"))
    assert not bot.requests


class Store[T](Protocol):
    def get(self) -> T: ...


class Options[T](TypedDict):
    value: T
    label: NotRequired[str]


@pytest.mark.parametrize("malformed", [None, "store", "options", "required"])
async def test_generic_protocol_and_typed_dict_dependencies_keep_their_objects(malformed):
    class Implementation:
        def get(self):
            return 7

    observed = []
    store = object() if malformed == "store" else Implementation()
    options = {} if malformed == "required" else {"value": "wrong" if malformed == "options" else 7}

    class Read(Feature):
        @command("read")
        async def read(self, *, store: Store[int], options: Options[int]) -> None:
            observed.append((store, options))

    bot = RecordingBot()
    async with App(Read(), data={"store": store, "options": options}) as app:
        assert app.check() == ()
        if malformed:
            with pytest.raises(ConfigurationError, match="does not match"):
                await app.feed_update(bot, incoming("/read"))
        else:
            await app.feed_update(bot, incoming("/read"))
    if not malformed:
        assert observed[0][0] is store
        assert observed[0][1] is options
    assert not bot.requests


def test_unsupported_dependency_annotations_fail_check_inspect_and_router():
    class Invalid(Feature):
        @command("go")
        async def go(self, *, value: LiteralString) -> None:
            pass

    app = App(Invalid())
    assert [issue.code for issue in app.check()] == ["dependency-type"]
    assert app.inspect()["diagnostics"][0]["code"] == "dependency-type"
    with pytest.raises(CompilationError, match="supported native"):
        app.build_router()


@pytest.mark.parametrize(
    ("placement", "policy", "expected"),
    [
        ("attached", "prefer", "file body"),
        ("attached", True, "description"),
        ("reply", "prefer", "print(1)"),
        ("reply-empty", "prefer", "file body"),
        ("reply-empty", True, "old caption"),
    ],
)
async def test_document_preference_is_within_each_source(placement, policy, expected):
    observed = []

    class Compiler(Feature):
        @command("code", code=TextInput(document=policy))
        async def code(self, code: str) -> None:
            observed.append(code)

    document = Document(file_id="code", file_unique_id="source", file_name="code.py", mime_type="text/plain")
    if placement == "attached":
        update = incoming(caption="/code description", document=document)
    else:
        reply = incoming(caption="old caption", document=document).message
        update = incoming("/code print(1)" if placement == "reply" else "/code", reply_to_message=reply)
    bot = RecordingBot()

    async def download(file, *, destination, timeout):
        destination.write(b"file body")

    bot.download = AsyncMock(side_effect=download)
    async with App(Compiler()) as app:
        await app.feed_update(bot, update)
    assert observed == [expected]
    assert bot.download.await_count == (1 if expected == "file body" else 0)
