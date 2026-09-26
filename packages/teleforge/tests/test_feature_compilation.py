import inspect
import json

import pytest
from aiogram.filters.callback_data import CallbackData

from teleforge.app import App
from teleforge.context import CallbackContext, MessageContext
from teleforge.declarations import callback, command, declarations_of, disable, event
from teleforge.feature import CompilationError, Feature
from teleforge.inputs import TextInput


class BaseTools(Feature, key="base"):
    @command("one")
    async def first(self, ctx: MessageContext, amount: int = 3) -> str:
        return str(amount)

    @command("two")
    async def second(self, ctx: MessageContext) -> str:
        return "base"


class DerivedTools(BaseTools, key="derived"):
    async def first(self, ctx: MessageContext, amount: int = 5) -> str:
        return str(amount + 1)

    @disable
    async def second(self, ctx: MessageContext) -> str:
        return "still callable"

    @command("three")
    async def third(self, ctx: MessageContext) -> str:
        return "new"


def test_override_preserves_triggers_order_signature_and_base() -> None:
    base = App(BaseTools()).iter_handlers()
    derived = App(DerivedTools()).iter_handlers()
    assert [(item.name, item.declaration.names) for item in derived] == [("first", ("one",)), ("third", ("three",))]
    assert derived[0].signature.parameters["amount"].default == 5
    assert [item.name for item in base] == ["first", "second"]
    assert declarations_of(DerivedTools().first) == declarations_of(BaseTools().first)
    assert not declarations_of(DerivedTools().second)


async def test_decorators_keep_methods_ordinary() -> None:
    feature = DerivedTools()
    assert await feature.first(None, 7) == "8"  # type: ignore[arg-type]
    assert await feature.second(None) == "still callable"  # type: ignore[arg-type]
    assert not hasattr(feature.first, "__wrapped__")
    assert inspect.iscoroutinefunction(feature.first)


def test_explicit_redeclaration_replaces_base_without_moving_route() -> None:
    class Different(BaseTools):
        @command("replacement")
        async def first(self, ctx: MessageContext, amount: int = 3) -> str:
            return "changed"

    handlers = App(Different()).iter_handlers()
    assert [(item.name, item.declaration.names) for item in handlers] == [
        ("first", ("replacement",)),
        ("second", ("two",)),
    ]


def test_errors_are_aggregate_source_located_and_do_not_open_lifespan() -> None:
    class Invalid(Feature):
        @command("broken", absent=TextInput())
        async def broken(self, text: str) -> str:
            return text

        @event("misspelled_event")
        async def invalid(self, ctx: MessageContext) -> None:
            pass

    app = App(Invalid(), Invalid())
    errors = app.check()
    assert {error.code for error in errors} == {"input-name", "event-name", "duplicate-feature"}
    assert all(error.source.line for error in errors if error.handler)
    with pytest.raises(CompilationError):
        app.build_router()


class Choice(CallbackData, prefix="choice"):
    item: int


def test_callback_payload_mismatch_is_a_compile_error() -> None:
    class Invalid(Feature):
        @callback(Choice)
        async def choose(self, ctx: CallbackContext, item: str) -> None:
            pass

    assert [error.code for error in App(Invalid()).check()] == ["payload-type"]


def test_manifest_is_structural_and_does_not_serialize_dependencies() -> None:
    class Secret:
        def __repr__(self) -> str:
            raise AssertionError("Must not inspect service values")

    app = App(DerivedTools(), data={"service": Secret()})
    manifest = app.inspect()
    assert manifest["dependencies"] == ["service"]
    assert "derived.first" in json.dumps(manifest)
    assert app._stack is None


def test_router_build_is_repeatable_for_embedding() -> None:
    app = App(BaseTools())
    first, second = app.build_router(), app.build_router()
    assert first is not second
    assert first.sub_routers[0] is not second.sub_routers[0]
    assert first.sub_routers[0].message.handlers[0].callback.__name__ == "first"


def test_multiple_entrypoints_keep_source_declaration_order() -> None:
    class Multi(Feature):
        @event("message")
        @event("edited_message")
        async def incoming(self, ctx: MessageContext) -> None:
            pass

    handlers = App(Multi()).iter_handlers()
    assert [item.declaration.event for item in handlers] == ["message", "edited_message"]
    assert len({item.key for item in handlers}) == 2


def test_inspection_exposes_effective_sources_and_unknown_external_dependencies() -> None:
    class Inputs(Feature):
        @command("describe", text=TextInput())
        async def describe(self, ctx: MessageContext, count: int, text: str, *, service: object) -> None:
            pass

    app = App(Inputs())
    compiled = app.iter_handlers()[0]
    parameters = app.inspect()["handlers"][0]["parameters"]
    assert (
        [(item["name"], item["source"]) for item in parameters]
        == [(parameter.name, parameter.source) for parameter in compiled.plan.parameters]
        == [("ctx", "context"), ("count", "argument"), ("text", "text"), ("service", "dependency")]
    )
    assert parameters[-1]["availability"] == "external"
    assert app.check() == ()  # Native middleware supplies external dependencies at invocation time.


def test_nonordinary_command_dependency_and_impossible_media_are_static_errors() -> None:
    from teleforge.inputs import Argument, ImageInput

    class Invalid(Feature):
        @command("service")
        async def service(self, service: object) -> None:
            pass

        @command("photo", image=ImageInput())
        async def photo(self, image: int) -> None:
            pass

        @event("message", count=Argument())
        async def incoming(self, count: int) -> None:
            pass

    assert {issue.code for issue in App(Invalid()).check()} == {"parameter-source", "input-type", "argument-source"}


def test_missing_card_renderer_is_rejected_by_check_inspect_and_router() -> None:
    from teleforge.cards import action

    class Invalid(Feature):
        @action(key="refresh", card="missing")
        async def refresh(self, ctx: CallbackContext) -> None:
            pass

    app = App(Invalid())
    assert [issue.code for issue in app.check()] == ["card-schema"]
    assert app.inspect()["diagnostics"][0]["code"] == "card-schema"
    with pytest.raises(CompilationError, match="missing"):
        app.build_router()


def test_step_and_job_signature_metadata_are_checked_without_binding_adapters() -> None:
    from pydantic import BaseModel

    from teleforge.conversations import step
    from teleforge.jobs import job

    class Draft(BaseModel):
        value: int

    class Invalid(Feature):
        @step("collect", draft=Draft)
        async def collect(self, ctx: MessageContext, draft: str) -> None:
            pass

        @job("work", payload=Draft)
        async def work(self, payload: str) -> None:
            pass

    assert {issue.code for issue in App(Invalid()).check()} == {"step-draft", "job-payload"}
