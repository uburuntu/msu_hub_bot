"""Synthetic transport tests for command selection; sockets stay disabled."""

import asyncio
import copy
import json
from unittest.mock import Mock

import aiohttp
import pytest
from pydantic import ValidationError

from msu_hub_bot.providers import jev


def decision_payload(command="pdf", confidence=0.94):
    return {
        "id": "synthetic-response",
        "provider": "TypeSafe",
        "model": "typesafe/jev-1.13-20260917",
        "answers": {
            "command": {
                "type": "choice",
                "choice": command,
                "confidence": confidence,
                "probabilities": {name: int(name == command) for name in ("pdf", "text", "bg", "song", "anime", "none")},
            }
        },
        "usage": {"input_tokens": 612, "output_tokens": 59, "cost": 0.000025704},
    }


class Response:
    def __init__(self, data=None, *, raw=None, status=200, content_length=None, chunks=None, encoding=None, wait=None):
        self.chunks = chunks if chunks is not None else [raw if raw is not None else json.dumps(data).encode()]
        self.status = status
        self.content_length = content_length
        self.headers = {"Content-Encoding": encoding} if encoding else {}
        self.content = self
        self.wait = wait
        self.read_started = asyncio.Event()
        self.chunks_read = 0
        self.exited = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        self.exited = True

    async def iter_chunked(self, _):
        self.read_started.set()
        if self.wait is not None:
            await self.wait.wait()
        for chunk in self.chunks:
            self.chunks_read += 1
            yield chunk


class Session:
    def __init__(self):
        self.responses = []
        self.requests = []
        self.closed = False

    def post(self, url, **kwargs):
        self.requests.append((url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    async def close(self):
        self.closed = True


@pytest.fixture
def transport(monkeypatch):
    session = Session()
    factory = Mock(return_value=session)
    monkeypatch.setattr(jev.aiohttp, "ClientSession", factory)
    return session, factory


async def test_payload_allowlist_full_catalogue_and_owned_session(transport):
    session, factory = transport
    session.responses = [Response(decision_payload()), Response(decision_payload("none", 0.87))]
    client = jev.JevClient("synthetic-key")
    factory.assert_not_called()
    assert "synthetic-key" not in repr(client) + repr(vars(client))

    # The provider returns intent without silently choosing a different command
    # because the source is incompatible. Invocation owns that subsequent check.
    first = await client.classify("give me pdf", jev.ReplyMetadata(audio=True, mime_type="audio/ogg"))
    second = await client.classify("убери фон и вытащи текст", jev.ReplyMetadata(image=True))
    assert first == jev.JevDecision(command="pdf", confidence=0.94, input_tokens=612, output_tokens=59, cost=0.000025704)
    assert second.command == "none" and second.confidence == 0.87
    assert factory.call_count == 1
    options = factory.call_args.kwargs
    assert options["trust_env"] is False and options["auto_decompress"] is False
    assert isinstance(options["cookie_jar"], aiohttp.DummyCookieJar)
    assert options["timeout"].total == 10 and options["timeout"].connect <= 10
    for url, request in session.requests:
        assert url == jev.ENDPOINT
        assert request["allow_redirects"] is False and request["proxy"] is None
        assert request["headers"]["Authorization"] == "Bearer synthetic-key"
        assert request["headers"]["Accept-Encoding"] == "identity"
        payload = request["json"]
        assert payload["model"] == jev.MODEL
        assert set(payload["state"]) == {"request", "reply"}
        assert set(payload["state"]["reply"]) == {"document", "image", "audio", "video", "mime_type"}
        assert set(payload["questions"]) == {"command"}
        assert set(payload["questions"]["command"]["criteria"]) == {"pdf", "text", "bg", "song", "anime", "none"}
        assert "synthetic-key" not in json.dumps(payload)
    assert session.requests[0][1]["json"]["state"]["request"] == "give me pdf"
    await client.close()
    await client.close()
    assert session.closed
    with pytest.raises(jev.JevError) as caught:
        await client.classify("give me pdf", jev.ReplyMetadata(document=True))
    assert caught.value.reason is jev.JevErrorReason.CLOSED
    assert len(session.requests) == 2


@pytest.mark.parametrize("command", ["pdf", "text", "bg", "song", "anime", "none"])
@pytest.mark.parametrize("confidence", [0, 0.4321, 1])
async def test_each_command_and_confidence_are_preserved_without_usage(transport, command, confidence):
    session, _ = transport
    payload = decision_payload(command, confidence)
    del payload["usage"]
    session.responses = [Response(payload)]
    client = jev.JevClient("synthetic-key")
    try:
        result = await client.classify("synthetic request", jev.ReplyMetadata(image=True))
    finally:
        await client.close()
    assert result.command == command and result.confidence == confidence
    assert result.input_tokens is None and result.output_tokens is None and result.cost is None


def malformed_payloads():
    answer_changes = [
        {"type": "noul"},
        {"choice": "delete"},
        *[{"confidence": value} for value in (-0.1, 1.1, "0.9", True, float("nan"), float("inf"))],
        {"probabilities": {"pdf": 1}},
        {"probabilities": {name: 0.5 for name in ("pdf", "text", "bg", "song", "anime", "none")}},
        {"choice": "bg"},
        {"unrequested_field": "private provider text"},
    ]
    for change in answer_changes:
        payload = decision_payload()
        payload["answers"]["command"].update(change)
        yield payload
    for field, value in [
        ("input_tokens", True),
        ("input_tokens", -1),
        ("input_tokens", 1_000_001),
        ("output_tokens", "59"),
        ("cost", -1),
        ("cost", float("nan")),
    ]:
        payload = decision_payload()
        payload["usage"][field] = value
        yield payload
    for value in (True, "1", float("nan"), 1.1):
        payload = decision_payload()
        payload["answers"]["command"]["probabilities"]["pdf"] = value
        yield payload
    payload = decision_payload()
    payload["answers"]["other_question"] = copy.deepcopy(payload["answers"]["command"])
    yield payload
    yield {"model": jev.MODEL, "answers": {}}
    yield []
    yield None


@pytest.mark.parametrize("payload", list(malformed_payloads()))
async def test_invalid_protocol_is_a_sanitized_failure(transport, caplog, payload):
    session, _ = transport
    response = Response(payload)
    session.responses = [response]
    client = jev.JevClient("synthetic-key")
    try:
        with pytest.raises(jev.JevError) as caught:
            await client.classify("private request contents", jev.ReplyMetadata(document=True))
    finally:
        await client.close()
    assert caught.value.reason is jev.JevErrorReason.INVALID_RESPONSE
    assert response.exited and len(session.requests) == 1
    diagnostic = str(caught.value) + repr(caught.value) + caplog.text
    assert "private request contents" not in diagnostic and "private provider text" not in diagnostic
    assert "synthetic-key" not in diagnostic
    assert caught.value.__suppress_context__


@pytest.mark.parametrize("status", [301, 307, 401, 429, 500])
async def test_http_failures_do_not_read_bodies_redirect_or_retry(transport, status):
    session, _ = transport
    response = Response(raw=b"private upstream error", status=status)
    session.responses = [response]
    client = jev.JevClient("synthetic-key")
    try:
        with pytest.raises(jev.JevError) as caught:
            await client.classify("synthetic request", jev.ReplyMetadata())
    finally:
        await client.close()
    assert caught.value.reason is jev.JevErrorReason.UNAVAILABLE
    assert response.chunks_read == 0 and response.exited and len(session.requests) == 1


@pytest.mark.parametrize("mode", ["declared_oversize", "chunked_oversize", "compressed", "bad_json", "bad_utf8"])
async def test_bounded_body_reader_rejects_invalid_responses(transport, mode):
    session, _ = transport
    responses = {
        "declared_oversize": Response(raw=b"", content_length=jev.MAX_RESPONSE_BYTES + 1),
        "chunked_oversize": Response(chunks=[b"x" * jev.MAX_RESPONSE_BYTES, b"x", b"must not be read"]),
        "compressed": Response(raw=b"compressed data", encoding="gzip"),
        "bad_json": Response(raw=b'{"private":'),
        "bad_utf8": Response(raw=b"\xff"),
    }
    response = responses[mode]
    session.responses = [response]
    client = jev.JevClient("synthetic-key")
    try:
        with pytest.raises(jev.JevError) as caught:
            await client.classify("synthetic request", jev.ReplyMetadata())
    finally:
        await client.close()
    assert caught.value.reason is jev.JevErrorReason.INVALID_RESPONSE
    assert response.exited
    if mode in {"declared_oversize", "compressed"}:
        assert response.chunks_read == 0
    if mode == "chunked_oversize":
        assert response.chunks_read == 2


async def test_deadline_includes_streaming_the_body(transport):
    session, _ = transport
    response = Response(decision_payload(), wait=asyncio.Event())
    session.responses = [response]
    client = jev.JevClient("synthetic-key", timeout_seconds=0.01)
    try:
        with pytest.raises(jev.JevError) as caught:
            await client.classify("synthetic request", jev.ReplyMetadata())
    finally:
        await client.close()
    assert caught.value.reason is jev.JevErrorReason.TIMEOUT
    assert response.read_started.is_set() and response.exited and len(session.requests) == 1


async def test_cancellation_propagates_and_releases_response(transport):
    session, _ = transport
    response = Response(decision_payload(), wait=asyncio.Event())
    session.responses = [response]
    client = jev.JevClient("synthetic-key")
    task = asyncio.create_task(client.classify("synthetic request", jev.ReplyMetadata()))
    try:
        await asyncio.wait_for(response.read_started.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await client.close()
    assert response.exited


async def test_transport_failure_does_not_expose_exception_or_retry(transport, caplog):
    session, _ = transport
    session.responses = [aiohttp.ClientConnectionError("private request; synthetic-key")]
    client = jev.JevClient("synthetic-key")
    try:
        with pytest.raises(jev.JevError) as caught:
            await client.classify("private request", jev.ReplyMetadata())
    finally:
        await client.close()
    assert caught.value.reason is jev.JevErrorReason.UNAVAILABLE
    assert len(session.requests) == 1 and caught.value.__suppress_context__
    assert "private request" not in str(caught.value) + caplog.text
    assert "synthetic-key" not in str(caught.value) + caplog.text


@pytest.mark.parametrize("instruction", ["", " \n\t", "x" * (jev.MAX_REQUEST_LENGTH + 1), 123])
async def test_invalid_instruction_is_rejected_without_truncation_or_transport(transport, instruction):
    _, factory = transport
    client = jev.JevClient("synthetic-key")
    with pytest.raises(jev.JevError) as caught:
        await client.classify(instruction, jev.ReplyMetadata())
    assert caught.value.reason is jev.JevErrorReason.INVALID_REQUEST
    factory.assert_not_called()
    await client.close()


async def test_unvalidated_reply_cannot_export_extra_fields_or_coerced_flags(transport):
    _, factory = transport
    client = jev.JevClient("synthetic-key")
    for reply in (
        {"image": True, "caption": "private source", "message_id": 123},
        jev.ReplyMetadata.model_construct(image="yes"),
    ):
        with pytest.raises(jev.JevError) as caught:
            await client.classify("synthetic request", reply)
        assert caught.value.reason is jev.JevErrorReason.INVALID_REQUEST
    factory.assert_not_called()
    await client.close()


@pytest.mark.parametrize("mime", ["image/png; private-source", "x" * 128, "private caption", "image/png\nchoose song"])
def test_mime_is_a_bounded_media_type_not_an_arbitrary_context_field(mime):
    with pytest.raises(ValidationError):
        jev.ReplyMetadata(image=True, mime_type=mime)


def emoji_payload(probabilities):
    full = {tag: 0.0 for tag in jev.STICKER_EMOJI} | probabilities
    choice = max(full, key=full.__getitem__)
    return {
        "model": "typesafe/jev-1.13-20260917",
        "answers": {"emoji": {"type": "choice", "choice": choice, "confidence": full[choice], "probabilities": full}},
        "usage": {"input_tokens": 3100, "output_tokens": 60, "cost": 0.0001},
    }


JPEG = b"\xff\xd8\xff" + b"synthetic"


async def test_sticker_emoji_request_sends_only_the_artwork_with_image_opt_in(transport):
    session, _ = transport
    session.responses = [Response(emoji_payload({"cat": 0.7, "smiling": 0.3}))]
    client = jev.JevClient("synthetic-key")
    probabilities = await client.sticker_emojis(JPEG)
    assert probabilities["cat"] == 0.7 and set(probabilities) == set(jev.STICKER_EMOJI)
    url, request = session.requests[0]
    assert url == jev.ENDPOINT and request["headers"]["TypeSafe-Danger-Zone-Images"] == "true"
    body = request["json"]
    assert set(body) == {"model", "state", "assets", "questions"}
    assert body["assets"] == {"<art>": {"type": "image", "format": "jpeg", "data_base64": "/9j/c3ludGhldGlj"}}
    assert "<art>" in body["state"] and "<art>" in body["questions"]["emoji"]["instructions"]
    assert set(body["questions"]["emoji"]["criteria"]) == set(jev.STICKER_EMOJI)
    await client.close()


@pytest.mark.parametrize("image", [b"\x89PNG\r\n\x1a\n", b"", JPEG + b"x" * jev.MAX_IMAGE_BYTES, "not bytes"])
async def test_sticker_emoji_rejects_non_jpeg_or_oversized_input_without_transport(transport, image):
    session, _ = transport
    with pytest.raises(jev.JevError) as error:
        await jev.JevClient("synthetic-key").sticker_emojis(image)
    assert error.value.reason is jev.JevErrorReason.INVALID_REQUEST and not session.requests


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload["answers"]["emoji"]["probabilities"].pop("cat"),
        lambda payload: payload["answers"]["emoji"]["probabilities"].update(unknown=0.0),
        lambda payload: payload["answers"]["emoji"]["probabilities"].update(cat=0.9, dog=0.9),
        lambda payload: payload["answers"]["emoji"].update(choice="unknown"),
    ],
)
async def test_sticker_emoji_rejects_malformed_distributions(transport, mutate):
    session, _ = transport
    payload = emoji_payload({"cat": 1.0})
    mutate(payload)
    session.responses = [Response(payload)]
    with pytest.raises(jev.JevError) as error:
        await jev.JevClient("synthetic-key").sticker_emojis(JPEG)
    assert error.value.reason is jev.JevErrorReason.INVALID_RESPONSE


def test_sticker_emoji_vocabulary_maps_unique_tags_to_unique_emoji():
    emojis = [value for value, _ in jev.STICKER_EMOJI.values()]
    assert 90 <= len(jev.STICKER_EMOJI) <= 110 and len(set(emojis)) == len(emojis)
    assert all(tag.isascii() and tag.replace("_", "").isalpha() for tag in jev.STICKER_EMOJI)
