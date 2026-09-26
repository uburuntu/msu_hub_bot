import io
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from aiogram.types import CallbackQuery, Chat, Document, Message, PhotoSize, User, Video

from teleforge.context import context_for
from teleforge.inputs import Argument, DocumentInput, ImageInput, InputError, MediaInput, TextInput, prepare_arguments
from teleforge.testing import RecordingBot


def message(**values):
    return Message(
        **{
            "message_id": 10,
            "date": datetime.now(UTC),
            "chat": Chat(id=-100123, type="supergroup"),
            "from_user": User(id=7, is_bot=False, first_name="Reader"),
            **values,
        }
    )


@pytest.mark.asyncio
async def test_typed_arguments_default_clamp_and_text_remainder():
    async def handler(digits: int = 3, text: str = ""):
        pass

    event = message(text="/roll 101 hello world")
    ctx = context_for(RecordingBot(), event)
    rules = {"digits": Argument(clamp=(1, 100)), "text": TextInput()}
    async with prepare_arguments(handler, event, ctx, {}, rules, tail="101 hello world") as values:
        assert values == {"digits": 100, "text": "hello world"}
    async with prepare_arguments(handler, event, ctx, {}, rules, tail="oops hello") as values:
        assert values == {"digits": 3, "text": "oops hello"}
    with pytest.raises(InputError, match="Invalid value"):
        async with prepare_arguments(handler, event, ctx, {}, {"digits": Argument(strict=True)}, tail="oops"):
            pytest.fail("Invalid strict arguments reached the handler")


@pytest.mark.asyncio
async def test_command_text_and_reply_photo_have_independent_provenance():
    async def handler(text: str, image: PhotoSize):
        pass

    source = message(message_id=5, photo=[PhotoSize(file_id="photo", file_unique_id="p", width=2, height=2)])
    event = message(text="/meme Hello", reply_to_message=source)
    bot = RecordingBot()
    ctx = context_for(bot, event)
    async with prepare_arguments(
        handler, event, ctx, {}, {"text": TextInput(), "image": ImageInput()}, tail="Hello"
    ) as values:
        assert values["text"] == "Hello"
        assert values["image"].file_id == "photo"
        assert ctx.input_sources == {"text": event, "image": source}
        assert ctx.response_target == source
        assert bot.requests == []  # Native media does not trigger an eager download.


@pytest.mark.asyncio
async def test_empty_command_tail_does_not_become_input_text():
    async def handler(text: str):
        pass

    event = message(text="/meme")
    with pytest.raises(InputError, match="Provide text"):
        async with prepare_arguments(
            handler, event, context_for(RecordingBot(), event), {}, {"text": TextInput()}, tail=""
        ):
            pytest.fail("Command token was interpreted as user text")


@pytest.mark.asyncio
async def test_callback_does_not_treat_card_as_input_or_apply_command_defaults():
    async def handler(text: str = "", count: int = 3):
        pass

    event = CallbackQuery(
        id="q", from_user=User(id=9, is_bot=False, first_name="Actor"), chat_instance="c", message=message(text="UI")
    )
    ctx = context_for(RecordingBot(), event)
    async with prepare_arguments(handler, event, ctx, {}, {"text": TextInput()}, payload={"count": 4}) as values:
        assert values == {"text": "", "count": 4}
        assert ctx.input_sources == {}
    with pytest.raises(InputError, match="no longer valid"):
        async with prepare_arguments(handler, event, ctx, {}, {"text": TextInput()}, payload={"count": "4"}):
            pytest.fail("Unvalidated callback data was coerced")


@pytest.mark.asyncio
async def test_downloads_are_bounded_deduplicated_and_owned_through_delivery():
    async def handler(stream: io.BytesIO, file: Path):
        pass

    attachment = Document(file_id="doc", file_unique_id="d", file_name="notes.txt")
    event = message(document=attachment)
    bot = RecordingBot()

    async def download(file, *, destination, timeout):
        destination.write(b"hello")

    bot.download = AsyncMock(side_effect=download)
    ctx = context_for(bot, event)
    with pytest.raises(RuntimeError, match="delivery failed"):
        async with prepare_arguments(
            handler, event, ctx, {}, {"stream": DocumentInput(), "file": DocumentInput()}
        ) as values:
            stream, path = values["stream"], values["file"]
            assert stream.getvalue() == path.read_bytes() == b"hello"
            assert bot.download.await_count == 1
            raise RuntimeError("delivery failed")
    assert stream.closed
    assert not path.exists()

    with pytest.raises(InputError, match="too large"):
        async with prepare_arguments(
            handler, event, ctx, {}, {"stream": DocumentInput(max_bytes=4), "file": DocumentInput()}
        ):
            pytest.fail("An unknown-size attachment exceeded the streaming budget")


@pytest.mark.asyncio
async def test_decoded_image_is_closed_after_invocation():
    from PIL import Image as images
    from PIL.Image import Image

    async def handler(image: Image):
        pass

    encoded = io.BytesIO()
    images.new("RGB", (3, 2)).save(encoded, format="PNG")
    bot = RecordingBot()

    async def download(file, *, destination, timeout):
        destination.write(encoded.getvalue())

    bot.download = AsyncMock(side_effect=download)
    event = message(photo=[PhotoSize(file_id="photo", file_unique_id="p", width=3, height=2)])
    async with prepare_arguments(handler, event, context_for(bot, event), {}, {"image": ImageInput()}) as values:
        image = values["image"]
        assert image.size == (3, 2)
        assert image.getpixel((0, 0)) == (0, 0, 0)
    with pytest.raises(ValueError, match="closed"):
        image.getpixel((0, 0))


@pytest.mark.asyncio
async def test_native_media_respects_known_size_budget_without_downloading():
    async def handler(image: PhotoSize):
        pass

    event = message(photo=[PhotoSize(file_id="photo", file_unique_id="p", width=3, height=2, file_size=100)])
    bot = RecordingBot()
    with pytest.raises(InputError, match="too large"):
        async with prepare_arguments(handler, event, context_for(bot, event), {}, {"image": ImageInput(max_bytes=1)}):
            pytest.fail("Oversized native input reached handler")
    assert not bot.requests


@pytest.mark.asyncio
@pytest.mark.parametrize("subtype", ["jpeg", "png", "tiff", "bmp", "gif", "webp"])
async def test_image_document_acquisition_keeps_supported_raster_types(subtype):
    async def handler(media: Document):
        pass

    attachment = Document(file_id="image", file_unique_id="i", mime_type=f"image/{subtype}")
    event = message(document=attachment)
    bot = RecordingBot()
    async with prepare_arguments(
        handler, event, context_for(bot, event), {}, {"media": MediaInput(kinds=("image",))}
    ) as values:
        assert values["media"] is attachment
    assert not bot.requests


@pytest.mark.asyncio
@pytest.mark.parametrize("rule", [DocumentInput(), MediaInput()])
async def test_raw_document_acquisition_still_accepts_svg(rule):
    async def handler(media: Document):
        pass

    attachment = Document(file_id="svg", file_unique_id="s", mime_type="image/svg+xml")
    event = message(document=attachment)
    bot = RecordingBot()
    async with prepare_arguments(handler, event, context_for(bot, event), {}, {"media": rule}) as values:
        assert values["media"] is attachment
    assert not bot.requests


@pytest.mark.asyncio
@pytest.mark.parametrize("kinds,expected", [(("video", "image"), "video"), (("image", "video"), "photo")])
async def test_declared_media_kind_order_selects_within_one_rich_message(kinds, expected):
    async def handler(media: PhotoSize | Video):
        pass

    event = message(
        rich_message={
            "blocks": [
                {"type": "photo", "photo": [{"file_id": "photo", "file_unique_id": "p", "width": 20, "height": 20}]},
                {
                    "type": "video",
                    "video": {"file_id": "video", "file_unique_id": "v", "width": 20, "height": 20, "duration": 1},
                },
            ]
        }
    )
    bot = RecordingBot()
    ctx = context_for(bot, event)
    async with prepare_arguments(handler, event, ctx, {}, {"media": MediaInput(kinds=kinds)}) as values:
        assert values["media"].file_id == expected
        assert ctx.input_sources["media"] is event
    assert not bot.requests


@pytest.mark.asyncio
async def test_retained_buffer_does_not_mask_handler_exception():
    async def handler(stream: io.BytesIO):
        pass

    bot = RecordingBot()

    async def download(file, *, destination, timeout):
        destination.write(b"hello")

    bot.download = AsyncMock(side_effect=download)
    event = message(document=Document(file_id="doc", file_unique_id="d"))
    primary = RuntimeError("original handler failure")
    with pytest.raises(RuntimeError) as caught:
        async with prepare_arguments(
            handler, event, context_for(bot, event), {}, {"stream": DocumentInput()}
        ) as values:
            retained = values["stream"].getbuffer()
            raise primary
    assert caught.value is primary
    assert primary.__notes__ == ["Input cleanup also failed (BufferError)"]
    retained.release()
    values["stream"].close()


@pytest.mark.asyncio
async def test_compiled_command_cannot_be_shadowed_by_middleware():
    from teleforge.declarations import Declaration
    from teleforge.parameters import compile_parameters

    async def handler(count: int = 3, *, label: str = "default"):
        pass

    plan, issues = compile_parameters(handler, Declaration(kind="command", event="message"))
    assert not issues
    event = message(text="/roll 9")
    async with prepare_arguments(
        handler,
        event,
        context_for(RecordingBot(), event),
        {"count": "unvalidated", "label": "injected"},
        {},
        tail="9",
        plan=plan,
    ) as values:
        assert values == {"count": 9, "label": "injected"}


@pytest.mark.asyncio
async def test_noncommand_native_values_never_parse_an_ambient_command_tail():
    from teleforge.declarations import Declaration
    from teleforge.issues import ConfigurationError
    from teleforge.parameters import compile_parameters

    async def handler(count: int = 3):
        pass

    plan, issues = compile_parameters(handler, Declaration(kind="event", event="message"))
    assert not issues
    event = message(text="ordinary message")
    ctx = context_for(RecordingBot(), event)
    async with prepare_arguments(handler, event, ctx, {}, {}, tail="9", plan=plan) as values:
        assert values == {"count": 3}
    with pytest.raises(ConfigurationError, match="does not match"):
        async with prepare_arguments(handler, event, ctx, {"count": "9"}, {}, plan=plan):
            pytest.fail("An injected scalar was coerced")


@pytest.mark.asyncio
async def test_injected_models_keep_identity_and_do_not_rerun_validators():
    from pydantic import BaseModel, model_validator

    from teleforge.parameters import checked_dependency

    validations = []

    class Service(BaseModel):
        name: str

        @model_validator(mode="after")
        def record(self):
            validations.append(self.name)
            return self

    service = Service(name="service")
    assert checked_dependency(Service, service, "service") is service
    assert validations == ["service"]


def test_structured_issues_only_accept_safe_declared_parameters():
    from teleforge.issues import ConfigurationError

    issue = InputError("text-too-long", limit=12)
    assert issue.code == "text-too-long"
    assert dict(issue.params) == {"limit": 12}
    assert "12 characters" in str(issue)
    with pytest.raises(ConfigurationError):
        InputError("argument-invalid", parameter="raw user input")
    with pytest.raises(ConfigurationError):
        InputError("argument-invalid", parameter="count", value="private")
