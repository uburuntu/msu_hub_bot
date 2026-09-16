"""Provider replies are synthetic; no Telegram or provider requests are sent."""

import ast
import asyncio
import importlib
import io
import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import ClientError

from common.externals.exceptions import ExternalServiceError
from hub_bot.commands import lingvanex


@pytest.fixture
def external_handlers():
    path = Path(__file__).resolve().parents[1] / "hub_bot/commands/externals.py"
    tree = ast.parse(path.read_text())
    # Isolate application globals and unrelated native conversion, not handler logic.
    tree.body = [
        node for node in tree.body
        if not isinstance(node, ast.ImportFrom) or node.module not in {"app", "utils.ffmpeg"}
    ]
    namespace = {}
    exec(compile(tree, str(path), "exec"), namespace)

    @asynccontextmanager
    async def no_chat_action(*args):
        yield

    namespace["ChatActioner"] = no_chat_action
    input_file = namespace["InputFile"]
    namespace["InputFile"] = SimpleNamespace(
        from_url=lambda url, filename: input_file(io.BytesIO(b"synthetic video"), filename=filename),
    )
    return namespace


@pytest.mark.parametrize("count", [0, 1, 2, 3, 5])
async def test_anime_results_use_valid_delivery(external_handlers, count):
    target = SimpleNamespace(reply=AsyncMock(), reply_video=AsyncMock(), reply_media_group=AsyncMock())
    message = SimpleNamespace(chat=object())
    external_handlers["extract_image"] = AsyncMock(return_value=(target, object()))
    external_handlers["download"] = AsyncMock(return_value=io.BytesIO(b"image"))
    external_handlers["which_anime"] = AsyncMock(return_value={"result": [
        {"filename": f"Episode <{i}>", "anilist": i, "similarity": 0.95, "video": f"https://example.org/{i}.mp4"}
        for i in range(count)
    ]})

    await external_handlers["process_which_anime"](message)

    if count == 0:
        target.reply.assert_awaited_once()
        assert "Не удалось найти аниме" in target.reply.call_args.args[0]
        target.reply_video.assert_not_awaited()
        target.reply_media_group.assert_not_awaited()
    elif count == 1:
        target.reply.assert_not_awaited()
        target.reply_video.assert_awaited_once()
        target.reply_media_group.assert_not_awaited()
        assert "Episode &lt;0&gt;" in target.reply_video.call_args.kwargs["caption"]
    else:
        target.reply.assert_not_awaited()
        target.reply_video.assert_not_awaited()
        target.reply_media_group.assert_awaited_once()
        media = target.reply_media_group.call_args.args[0].media
        assert len(media) == min(count, 3)
        assert "Episode &lt;0&gt;" in media[0].caption
        assert all(not item.caption for item in media[1:])


def translation_input(*, image=True, text="Original"):
    target = SimpleNamespace(reply=AsyncMock())
    meta = SimpleNamespace(
        arguments=["en", "ru"],
        extract_image_with_downloading=AsyncMock(return_value=(target, io.BytesIO(b"image") if image else None)),
        extract_text=lambda: (target, text),
    )
    return target, meta


@pytest.mark.parametrize("handler", ["process_en", "process_ru", "process_translate"])
@pytest.mark.parametrize("failure", [ExternalServiceError, ClientError, TimeoutError])
async def test_translation_failures_receive_one_reply(monkeypatch, handler, failure):
    target, meta = translation_input()
    monkeypatch.setattr(lingvanex, "translate_image", AsyncMock(side_effect=failure("synthetic provider failure")))
    monkeypatch.setattr(lingvanex, "translate", AsyncMock(side_effect=failure("synthetic provider failure")))

    await getattr(lingvanex, handler)(target, meta)

    target.reply.assert_awaited_once_with("Не удалось выполнить перевод. Попробуйте ещё раз позже.")
    lingvanex.translate_image.assert_awaited_once()
    lingvanex.translate.assert_awaited_once()


@pytest.mark.parametrize("failed", ["image", "text"])
async def test_translation_preserves_partial_result_and_names_failure(monkeypatch, failed):
    target, meta = translation_input()
    image = AsyncMock(return_value="Picture <text>")
    text = AsyncMock(return_value="Translated <text>")
    (image if failed == "image" else text).side_effect = ExternalServiceError("synthetic failure")
    monkeypatch.setattr(lingvanex, "translate_image", image)
    monkeypatch.setattr(lingvanex, "translate", text)

    await lingvanex.process_ru(target, meta)

    target.reply.assert_awaited_once()
    reply = target.reply.call_args.args[0]
    assert ("Translated &lt;text&gt;" if failed == "image" else "Picture &lt;text&gt;") in reply
    assert ("Не удалось перевести изображение." if failed == "image" else "Не удалось перевести текст.") in reply


async def test_translation_success_keeps_both_escaped_parts(monkeypatch):
    target, meta = translation_input()
    monkeypatch.setattr(lingvanex, "translate_image", AsyncMock(return_value="Picture <text>"))
    monkeypatch.setattr(lingvanex, "translate", AsyncMock(return_value="Translated & text"))

    await lingvanex.process_ru(target, meta)

    target.reply.assert_awaited_once_with("Picture &lt;text&gt;\n\nTranslated &amp; text")


async def test_translation_without_input_does_not_call_provider(monkeypatch):
    target, meta = translation_input(image=False, text="")
    monkeypatch.setattr(lingvanex, "translate_image", AsyncMock())
    monkeypatch.setattr(lingvanex, "translate", AsyncMock())

    await lingvanex.process_ru(target, meta)

    target.reply.assert_not_awaited()
    lingvanex.translate_image.assert_not_awaited()
    lingvanex.translate.assert_not_awaited()


@pytest.mark.parametrize("result", ["", " \n\t"])
async def test_empty_translation_result_does_not_send_blank_message(monkeypatch, result):
    target, meta = translation_input(text="")
    monkeypatch.setattr(lingvanex, "translate_image", AsyncMock(return_value=result))

    await lingvanex.process_ru(target, meta)

    target.reply.assert_awaited_once_with("Не удалось выполнить перевод. Попробуйте ещё раз позже.")


async def test_translation_cancellation_propagates(monkeypatch):
    target, meta = translation_input()
    monkeypatch.setattr(lingvanex, "translate_image", AsyncMock(side_effect=asyncio.CancelledError))

    with pytest.raises(asyncio.CancelledError):
        await lingvanex.process_ru(target, meta)

    target.reply.assert_not_awaited()


@pytest.fixture(params=["fakeyou", "topdf"])
def pending_provider(request, monkeypatch):
    module = importlib.import_module(f"common.externals.{request.param}")
    original_sleep = asyncio.sleep

    async def yield_to_loop(_delay):
        await original_sleep(0)

    class Response:
        status = 200

        def __init__(self, data):
            self.data = data

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def json(self):
            return self.data

        async def read(self):
            return json.dumps(self.data).encode()

    class Session:
        closed = False
        complete = False

        def __init__(self):
            self.polling = asyncio.Event()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            self.closed = True

        def post(self, *args, **kwargs):
            return Response({"inference_job_token": "synthetic-job"})

        def get(self, url, **kwargs):
            if "/convert/" in url:
                return Response({})
            self.polling.set()
            if request.param == "fakeyou":
                return Response({"state": {
                    "status": "complete_success" if self.complete else "pending",
                    "maybe_public_bucket_wav_audio_path": "/synthetic.wav",
                }})
            return Response({
                "status": "ready" if self.complete else "processing",
                "convert_result": "synthetic.pdf",
                "thumb_url": "synthetic.png",
            })

    session = Session()
    monkeypatch.setattr(module.aiohttp, "ClientSession", lambda **kwargs: session)
    monkeypatch.setattr(module.asyncio, "sleep", yield_to_loop)

    async def invoke():
        if request.param == "fakeyou":
            return await module.fake_you("Synthetic sentence")
        return await module.convert_to_pdf(io.BytesIO(b"document"), "synthetic.txt", "text/plain")

    return module, session, invoke


async def test_provider_polling_deadline_closes_session(pending_provider, monkeypatch):
    module, session, invoke = pending_provider
    monkeypatch.setattr(module, "JOB_TIMEOUT_SECONDS", 0.02)
    started = asyncio.get_running_loop().time()

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(invoke(), 1)

    assert asyncio.get_running_loop().time() - started < 0.5
    assert session.polling.is_set()
    assert session.closed


async def test_provider_caller_cancellation_closes_session(pending_provider):
    _, session, invoke = pending_provider
    task = asyncio.create_task(invoke())
    try:
        await asyncio.wait_for(session.polling.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert session.closed
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_provider_ready_result_still_returns(pending_provider):
    module, session, invoke = pending_provider
    session.complete = True

    result = await asyncio.wait_for(invoke(), 1)

    assert session.closed
    if module.__name__.endswith("fakeyou"):
        assert result.endswith("/synthetic.wav")
    else:
        assert result[0].endswith("/synthetic.pdf")
        assert result[2] == "synthetic.pdf"


@pytest.mark.parametrize("kind", ["voice", "pdf"])
async def test_job_timeout_has_actionable_command_reply(external_handlers, kind):
    target = SimpleNamespace(reply=AsyncMock(), chat=object())
    if kind == "voice":
        meta = SimpleNamespace(extract_text=lambda: (target, "Synthetic sentence"), keyword="homer")
        external_handlers["translate"] = AsyncMock(return_value="Synthetic sentence")
        external_handlers["fake_you"] = AsyncMock(side_effect=TimeoutError)
        await external_handlers["process_fake_voice"](target, meta)
        assert "Озвучка заняла слишком много времени" in target.reply.call_args.args[0]
    else:
        document = SimpleNamespace(file_name="synthetic.txt", mime_type="text/plain")
        meta = SimpleNamespace(extract_doc=AsyncMock(return_value=(target, document)))
        external_handlers["download"] = AsyncMock(return_value=io.BytesIO(b"document"))
        external_handlers["convert_to_pdf"] = AsyncMock(side_effect=TimeoutError)
        await external_handlers["process_topdf"](target, meta)
        assert "Конвертация заняла слишком много времени" in target.reply.call_args.args[0]
    target.reply.assert_awaited_once()
