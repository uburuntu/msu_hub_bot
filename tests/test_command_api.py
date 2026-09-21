"""Typed commands retain native dispatch, provenance and invocation ownership."""

import asyncio
import io
import inspect
import threading
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from aiogram import BaseMiddleware, Dispatcher, Router
from aiogram.filters import StateFilter
from aiogram.methods import SendDocument, SendMessage
from aiogram.types import Document, Message, PhotoSize, Update
from aiogram.utils.formatting import Pre, Text
from PIL import Image

from msu_hub_bot.telegram.command_api import (
    Argument,
    DocumentInput,
    ImageInput,
    MediaInput,
    MetaCommand,
    MetaInfo,
    TextInput,
    invoke_command,
    register_command,
)
from msu_hub_bot.telegram.command_api import acquisition
from msu_hub_bot.telegram.files import DownloadableMedia
from msu_hub_bot.telegram.filters import MetaCommand as CommandFilter
from telegram_helpers import make_bot, make_message


@pytest.fixture
async def bot():
    bot = make_bot()
    yield bot
    await bot.session.close()


@pytest.mark.parametrize("tail,expected", [("", 3), ("bad", 3), ("5", 5), ("-5", 1), ("1000", 100)])
async def test_scalars_use_signature_defaults_and_explicit_clamp(bot, tail, expected):
    received = []

    @MetaCommand("roll", digits=Argument(clamp=(1, 100)))
    async def roll(digits: int = 3) -> Text:
        received.append(digits)
        return Text(str(digits))

    original = roll
    result = await invoke_command(roll, make_message(bot, text=f"/roll {tail}"))
    assert received == [expected]
    assert isinstance(result, list) and all(isinstance(item, Message) for item in result)
    assert roll is original and inspect.signature(roll).parameters["digits"].annotation is int
    assert not hasattr(roll, "__wrapped__")


@pytest.mark.parametrize("required,strict", [(True, False), (False, True)])
async def test_invalid_required_or_strict_argument_is_guidance_after_matching(bot, required, strict):
    reached = []
    if required:

        @MetaCommand("number")
        async def command(number: int) -> None:
            reached.append(number)
    else:

        @MetaCommand("number", number=Argument(strict=True))
        async def command(number: int = 3) -> None:
            reached.append(number)

    router = Router()
    register_command(router.message, command)

    @router.message()
    async def fallback(message: Message) -> None:
        reached.append("fallback")

    dispatcher = Dispatcher(disable_fsm=True)
    dispatcher.include_router(router)
    message = make_message(bot, text="/number invalid")
    await dispatcher.feed_update(bot, Update(update_id=1, message=message))
    assert reached == []
    assert len(bot.session.methods) == 1
    assert isinstance(bot.session.methods[0], SendMessage)
    assert "Использование: /number" in bot.session.methods[0].text


async def test_strict_still_parses_valid_textual_tokens(bot):
    received = []

    @MetaCommand("number", number=Argument(strict=True))
    async def command(number: int = 3) -> None:
        received.append(number)

    await invoke_command(command, make_message(bot, text="/number 5"))
    assert received == [5]


async def test_argument_and_text_sources_stay_distinct_and_failed_token_is_not_eaten(bot):
    received = []

    @MetaCommand("format", text=TextInput(reply=True))
    async def command(meta: MetaInfo, count: int = 3, text: str = "fallback") -> None:
        received.append((count, text, meta.raw_text, meta.arguments, meta.extract_text()))

    reply = make_message(bot, message_id=10, text="from reply")
    valid = make_message(bot, text="/format 5", reply_to_message=reply)
    await invoke_command(command, valid)
    invalid = make_message(bot, text="/format keep every word", reply_to_message=reply)
    await invoke_command(command, invalid)
    assert received[0] == (5, "from reply", "5", ["5"], (reply, "from reply"))
    assert received[1] == (3, "keep every word", "keep every word", ["keep", "every", "word"], (invalid, "keep every word"))


async def test_text_parameter_without_declaration_is_an_ordinary_token(bot):
    received = []

    @MetaCommand("plain")
    async def command(text: str) -> None:
        received.append(text)

    await invoke_command(command, make_message(bot, text="/plain first second"))
    assert received == ["first"]


async def test_text_default_and_hard_limit_do_not_replace_oversized_content(bot):
    received = []

    @MetaCommand("figlet", text=TextInput(reply=True, max_chars=5), rich=False, soft_messages=1)
    async def command(text: str = "kek") -> Pre:
        received.append(text)
        return Pre(text)

    await invoke_command(command, make_message(bot, text="/figlet"))
    await invoke_command(command, make_message(bot, text="/figlet too long"))
    assert received == ["kek"]
    assert "максимум 5" in bot.session.methods[-1].text


async def test_missing_text_is_rejected_before_avatar_lookup(bot, monkeypatch):
    avatar = AsyncMock()
    monkeypatch.setattr(acquisition.SimpleExtractor, "profile_photo", avatar)

    @MetaCommand("meme", text=TextInput(), media=MediaInput(avatar=True))
    async def command(text: str, media: DownloadableMedia) -> None:
        pytest.fail("Missing required text reached the handler")

    await invoke_command(command, make_message(bot, text="/meme"))
    avatar.assert_not_awaited()
    assert "Использование: /meme" in bot.session.methods[0].text


async def test_caption_uses_invocation_text_parent_photo_and_parent_success_target(bot, monkeypatch):
    download = AsyncMock()
    monkeypatch.setattr(acquisition, "download", download)
    seen = []

    @MetaCommand("meme", text=TextInput(), media=MediaInput())
    async def command(meta: MetaInfo, text: str, media: DownloadableMedia) -> str:
        seen.append((text, media.file_id, meta.extract_text(), meta.reply_target(), meta.resolved.copy()))
        assert (await meta.extract_image())[0] is meta.reply_target()
        return "done"

    reply = make_message(bot, message_id=20, photo=[PhotoSize(file_id="photo", file_unique_id="p", width=10, height=10)])
    message = make_message(bot, text="/meme Hello", reply_to_message=reply)
    await invoke_command(command, message)
    assert seen[0][:4] == ("Hello", "photo", (message, "Hello"), reply)
    assert seen[0][4]["text"] == "Hello" and "meta" not in seen[0][4]
    assert bot.session.methods[0].reply_parameters.message_id == 20
    download.assert_not_awaited()


async def test_origin_media_beats_different_reply_type(bot):
    seen = []

    @MetaCommand("meme", media=MediaInput())
    async def command(media: DownloadableMedia) -> None:
        seen.append(media.file_id)

    reply = make_message(bot, video={"file_id": "video", "file_unique_id": "v", "width": 10, "height": 10, "duration": 1})
    message = make_message(
        bot, caption="/meme", photo=[PhotoSize(file_id="photo", file_unique_id="p", width=10, height=10)], reply_to_message=reply
    )
    await invoke_command(command, message)
    assert seen == ["photo"]


@pytest.mark.parametrize(
    "unsupported",
    [
        {"document": {"file_id": "doc", "file_unique_id": "d", "mime_type": "application/pdf"}},
        {"audio": {"file_id": "audio", "file_unique_id": "a", "duration": 1}},
        {"voice": {"file_id": "voice", "file_unique_id": "v", "duration": 1}},
    ],
)
async def test_caption_media_ignores_unsupported_files_and_falls_back_to_avatar(bot, monkeypatch, unsupported):
    photo = PhotoSize(file_id="avatar", file_unique_id="p", width=10, height=10)
    avatar = AsyncMock(return_value=photo)
    monkeypatch.setattr(acquisition.SimpleExtractor, "profile_photo", avatar)
    seen = []

    @MetaCommand("meme", media=MediaInput(kinds=("image", "video"), avatar=True))
    async def command(media: DownloadableMedia) -> None:
        seen.append(media)

    message = make_message(bot, caption="/meme", **unsupported)
    await invoke_command(command, message)
    assert seen == [photo]
    avatar.assert_awaited_once_with(message)


async def test_allowed_reply_media_precedes_avatar_when_origin_kind_is_excluded(bot, monkeypatch):
    avatar = AsyncMock()
    monkeypatch.setattr(acquisition.SimpleExtractor, "profile_photo", avatar)
    seen = []

    @MetaCommand("meme", media=MediaInput(kinds=("image", "video"), avatar=True))
    async def command(media: DownloadableMedia) -> None:
        seen.append(media.file_id)

    reply = make_message(bot, photo=[PhotoSize(file_id="photo", file_unique_id="p", width=10, height=10)])
    message = make_message(
        bot, caption="/meme", document=Document(file_id="doc", file_unique_id="d", mime_type="application/pdf"), reply_to_message=reply
    )
    await invoke_command(command, message)
    assert seen == ["photo"]
    avatar.assert_not_awaited()


async def test_resolver_gets_raw_request_named_di_and_can_change_selected_text(bot):
    marker = object()
    seen = []

    async def resolve(*, meta, source, target, text, catalogue):
        assert catalogue is marker
        assert (source, target, text) == ("переведи", "на", "русский")
        assert meta.raw_text == "переведи на русский"
        assert meta.context_messages == 5
        meta.input_sources["text"] = meta.message.reply_to_message
        return {"source": "en", "target": "ru", "text": meta.message.reply_to_message.text}

    @MetaCommand("tr", text=TextInput(), resolve=resolve, context_messages=5)
    async def command(source: str, target: str, text: str, meta: MetaInfo) -> str:
        seen.append((source, target, text, meta.extract_text(), meta.resolved.copy()))
        return "Привет"

    reply = make_message(bot, message_id=25, text="Hello")
    await invoke_command(command, make_message(bot, text="/tr переведи на русский", reply_to_message=reply), catalogue=marker)
    assert seen[0][:4] == ("en", "ru", "Hello", (reply, "Hello"))
    assert seen[0][4] == {"source": "en", "target": "ru", "text": "Hello"}
    assert bot.session.methods[0].reply_parameters.message_id == 25


async def test_resolver_runs_before_signature_defaults_and_guidance(bot):
    seen = []

    async def resolve(*, count):
        assert count is None
        return {"count": "7"}

    @MetaCommand("number", resolve=resolve)
    async def command(count: int = 3) -> None:
        seen.append(count)

    await invoke_command(command, make_message(bot, text="/number invalid"))
    assert seen == [7]


async def test_managed_path_lives_through_delivery_then_disappears(bot, monkeypatch):
    stream = io.BytesIO(b"document contents")
    mocked = AsyncMock(return_value=stream)
    monkeypatch.setattr(acquisition, "download", mocked)
    paths = []

    @MetaCommand("copy", document=DocumentInput(max_bytes=32), output="document")
    async def command(document: Path) -> Path:
        assert document.read_bytes() == b"document contents"
        paths.append(document)
        return document

    await invoke_command(
        command,
        make_message(bot, caption="/copy", document=Document(file_id="doc", file_unique_id="d", file_name="../../input.txt")),
    )
    assert len(paths) == 1 and not paths[0].exists() and stream.closed
    assert mocked.call_args.kwargs["max_bytes"] == 32
    assert isinstance(bot.session.methods[0], SendDocument)
    assert bot.session.methods[0].document.data == b"document contents"


async def test_cancellation_closes_managed_input_and_never_sends_guidance(bot, monkeypatch):
    stream = io.BytesIO(b"document contents")
    monkeypatch.setattr(acquisition, "download", AsyncMock(return_value=stream))
    started = asyncio.Event()
    paths = []

    @MetaCommand("copy", document=DocumentInput())
    async def command(document: Path) -> None:
        paths.append(document)
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(
        invoke_command(command, make_message(bot, caption="/copy", document=Document(file_id="doc", file_unique_id="d")))
    )
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stream.closed and not paths[0].exists()
    assert bot.session.methods == []


async def test_decoded_image_and_download_cache_share_source_and_close(bot, monkeypatch):
    payload = io.BytesIO()
    with Image.new("RGB", (2, 2), "red") as image:
        image.save(payload, "PNG")
    payload.seek(0)
    mocked = AsyncMock(return_value=payload)
    monkeypatch.setattr(acquisition, "download", mocked)
    images = []

    @MetaCommand("image", image=ImageInput())
    async def command(image: Image.Image, meta: MetaInfo) -> None:
        images.append(image)
        assert image.getpixel((0, 0)) == (255, 0, 0)
        source, cached = await meta.extract_image_with_downloading()
        assert source is meta.message and cached is payload

    await invoke_command(
        command, make_message(bot, caption="/image", photo=[PhotoSize(file_id="p", file_unique_id="p", width=2, height=2)])
    )
    mocked.assert_awaited_once()
    assert payload.closed
    with pytest.raises(ValueError):
        images[0].getpixel((0, 0))


async def test_native_adapter_keeps_flags_event_di_and_delivery_inside_middleware(bot, monkeypatch):
    from msu_hub_bot.telegram import responses

    seen = []

    class Service:
        pass

    service = Service()

    class Scope(BaseMiddleware):
        async def __call__(self, handler, event, data):
            seen.append("enter")
            try:
                return await handler(event, data)
            finally:
                seen.append("exit")

    async def send(target, text, **kwargs):
        seen.append(("send", text))
        return [make_message(bot)]

    monkeypatch.setattr(responses, "send_response", send)

    @MetaCommand("typed")
    async def command(number: int, service: Service, meta: MetaInfo) -> str:
        assert service is marker and meta.message.text == "/typed 6"
        seen.append(("body", number))
        return "result"

    marker = service
    router = Router()
    router.message.middleware(Scope())
    adapter = register_command(router.message, command, StateFilter(None), flags={"handler_key": "stable", "fsm_release": False})
    assert adapter.__qualname__ == command.__qualname__
    assert not hasattr(adapter, "__wrapped__")
    native = router.message.handlers[0]
    assert native.params == {"message"} and native.varkw
    assert native.flags == {"handler_key": "stable", "fsm_release": False}
    dispatcher = Dispatcher(disable_fsm=True, service=service)
    dispatcher.include_router(router)
    await dispatcher.feed_update(bot, Update(update_id=1, message=make_message(bot, text="/typed 6")))
    assert seen == ["enter", ("body", 6), ("send", "result"), "exit"]


@pytest.mark.parametrize("kind", ["message", "list", "none", "bool"])
async def test_already_delivered_returns_are_not_sent_twice(bot, kind):
    @MetaCommand("once")
    async def command(meta: MetaInfo) -> object:
        if kind == "message":
            return await meta.reply("once", fixed=True)
        if kind == "list":
            return await meta.reply("once")
        await meta.reply("once")
        return None if kind == "none" else True

    await invoke_command(command, make_message(bot, text="/once"))
    assert len(bot.session.methods) == 1


async def test_legacy_filter_raw_metadata_and_hashtag_grammar_are_preserved(bot):
    seen = []

    @MetaCommand("tr", "translate", text=TextInput())
    async def command(source: str, target: str, text: str) -> None:
        seen.append((source, target, text))

    router = Router()
    register_command(router.message, command)
    filter = router.message.handlers[0].filters[0].callback
    assert isinstance(filter, CommandFilter) and filter.args == 2 and filter.commands == ("tr", "translate")
    message = make_message(bot, text="prefix #translate_en_ru suffix")
    selected = await filter(message, bot)
    assert selected["meta"].arguments == ["en", "ru"]
    assert selected["meta"].text == "prefix  suffix"
    await invoke_command(command, message, **selected)
    assert seen == [("en", "ru", "prefix  suffix")]


def test_ambiguous_declarations_fail_before_registration():
    with pytest.raises(ValueError, match="no matching"):

        @MetaCommand("bad", typo=TextInput())
        async def unknown(text: str) -> None:
            pass

    with pytest.raises(TypeError, match="One text input"):

        @MetaCommand("bad", first=TextInput(), second=TextInput())
        async def ambiguous(first: str, second: str) -> None:
            pass

    with pytest.raises(TypeError, match="must be async"):

        @MetaCommand("bad")
        def blocking() -> None:
            pass


async def test_output_limit_guidance_uses_invocation_and_can_exceed_the_result_budget(bot):
    @MetaCommand("small", text=TextInput(), max_output_bytes=1)
    async def command(text: str) -> str:
        return text

    reply = make_message(bot, message_id=20, text="too long")
    await invoke_command(command, make_message(bot, text="/small", reply_to_message=reply))
    assert len(bot.session.methods) == 1
    assert bot.session.methods[0].reply_parameters.message_id == 1
    assert bot.session.methods[0].text != "too long"


async def test_delivery_failure_closes_owned_input_without_retrying_or_sending_guidance(bot, monkeypatch):
    from msu_hub_bot.telegram import responses

    stream = io.BytesIO(b"document contents")
    monkeypatch.setattr(acquisition, "download", AsyncMock(return_value=stream))
    paths = []

    async def failed(target, text=None, **kwargs):
        assert kwargs["document"].exists()
        raise RuntimeError("transport failed")

    send = AsyncMock(side_effect=failed)
    monkeypatch.setattr(responses, "send_response", send)

    @MetaCommand("copy", document=DocumentInput(), output="document")
    async def command(document: Path) -> Path:
        paths.append(document)
        return document

    with pytest.raises(RuntimeError, match="transport failed"):
        await invoke_command(command, make_message(bot, caption="/copy", document=Document(file_id="doc", file_unique_id="d")))
    assert stream.closed and not paths[0].exists()
    send.assert_awaited_once()
    assert bot.session.methods == []


async def test_optional_media_absence_does_not_require_a_conversation(bot):
    received = []

    @MetaCommand("image", image=ImageInput())
    async def command(image: PhotoSize | None = None) -> None:
        received.append(image)

    await invoke_command(command, make_message(bot, text="/image"))
    assert received == [None]
    assert bot.session.methods == []


async def test_text_document_is_downloaded_once_and_reused_by_legacy_extraction(bot, monkeypatch):
    stream = io.BytesIO("Текст из документа".encode())
    mocked = AsyncMock(return_value=stream)
    monkeypatch.setattr(acquisition, "download", mocked)
    seen = []

    @MetaCommand("text", text=TextInput(document=True, max_bytes=100))
    async def command(text: str, meta: MetaInfo) -> None:
        seen.append((text, await meta.extract_text_with_doc_plain()))

    message = make_message(bot, caption="/text", document=Document(file_id="doc", file_unique_id="d", mime_type="text/plain"))
    await invoke_command(command, message)
    assert seen == [("Текст из документа", (message, "Текст из документа"))]
    mocked.assert_awaited_once()
    assert stream.closed


async def test_cancelled_image_decode_keeps_stream_alive_until_worker_exits(bot, monkeypatch):
    payload = io.BytesIO()
    with Image.new("RGB", (2, 2)) as image:
        image.save(payload, "PNG")
    payload.seek(0)
    monkeypatch.setattr(acquisition, "download", AsyncMock(return_value=payload))
    original_open = Image.open
    started, release = threading.Event(), threading.Event()

    def blocked(stream):
        started.set()
        assert release.wait(2)
        assert not stream.closed
        return original_open(stream)

    monkeypatch.setattr(acquisition.Image, "open", blocked)

    @MetaCommand("image", image=ImageInput())
    async def command(image: Image.Image) -> None:
        pytest.fail("Cancelled acquisition reached the handler")

    task = asyncio.create_task(
        invoke_command(command, make_message(bot, caption="/image", photo=[PhotoSize(file_id="p", file_unique_id="p", width=2, height=2)]))
    )
    try:
        async with asyncio.timeout(2):
            while not started.is_set():
                await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not payload.closed
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not payload.closed
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert payload.closed and bot.session.methods == []
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_text_and_local_document_reuse_one_bounded_download(bot, monkeypatch):
    stream = io.BytesIO(b"document contents")
    mocked = AsyncMock(return_value=stream)
    monkeypatch.setattr(acquisition, "download", mocked)
    seen = []

    @MetaCommand("text", text=TextInput(document=True), document=DocumentInput())
    async def command(text: str, document: Path) -> None:
        seen.append((text, document.read_text()))

    await invoke_command(
        command, make_message(bot, caption="/text", document=Document(file_id="doc", file_unique_id="d", mime_type="text/plain"))
    )
    assert seen == [("document contents", "document contents")]
    mocked.assert_awaited_once()
    assert stream.closed


async def test_metadata_limits_remain_with_consumer_but_download_limits_apply_before_fetch(bot, monkeypatch):
    mocked = AsyncMock()
    monkeypatch.setattr(acquisition, "download", mocked)
    seen = []

    @MetaCommand("meta", document=DocumentInput(max_bytes=10))
    async def metadata(document: Document) -> None:
        seen.append(document.file_size)

    @MetaCommand("local", document=DocumentInput(max_bytes=10))
    async def local(document: Path) -> None:
        pytest.fail("Oversized download reached the handler")

    document = Document(file_id="doc", file_unique_id="d", file_size=11)
    await invoke_command(metadata, make_message(bot, caption="/meta", document=document))
    await invoke_command(local, make_message(bot, caption="/local", document=document))
    assert seen == [11]
    mocked.assert_not_awaited()
    assert "слишком большой" in bot.session.methods[0].text
