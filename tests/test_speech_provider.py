"""Synthetic transport tests for OpenRouter transcription; sockets stay disabled."""

import asyncio
import base64
import json
from unittest.mock import Mock

import aiohttp
import pytest

from msu_hub_bot.providers import speech
from msu_hub_bot.providers.exceptions import ExternalServiceError


class Response:
    def __init__(self, data=None, *, raw=None, status=200, content_length=None, encoding=None):
        self.chunks = [raw if raw is not None else json.dumps(data).encode()]
        self.status = status
        self.content_length = content_length
        self.headers = {"Content-Encoding": encoding} if encoding else {}
        self.content = self
        self.chunks_read = 0
        self.exited = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        self.exited = True

    async def iter_chunked(self, _):
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
    monkeypatch.setattr(speech.aiohttp, "ClientSession", factory)
    return session, factory


async def test_request_uses_inline_audio_private_routing_and_owned_session(transport):
    session, factory = transport
    session.responses = [
        Response({"text": "Привет, это голосовое", "usage": {"seconds": 9.2, "cost": 0.000069, "input_tokens": 83}}),
        Response({"text": "second"}),
    ]
    client = speech.SpeechClient("synthetic-key")
    factory.assert_not_called()
    assert "synthetic-key" not in repr(client) + repr(vars(client))

    first = await client.transcribe(b"mp3-bytes")
    second = await client.transcribe(b"more")

    assert first == speech.Transcript(text="Привет, это голосовое", seconds=9.2, cost=0.000069)
    assert second.text == "second" and second.seconds is None and second.cost is None
    assert factory.call_count == 1
    options = factory.call_args.kwargs
    assert options["trust_env"] is False and options["auto_decompress"] is False
    assert isinstance(options["cookie_jar"], aiohttp.DummyCookieJar)
    assert options["timeout"].total == 120
    for url, request in session.requests:
        assert url == speech.ENDPOINT
        assert request["allow_redirects"] is False and request["proxy"] is None
        assert request["headers"]["Authorization"] == "Bearer synthetic-key"
        assert request["headers"]["Accept-Encoding"] == "identity"
        payload = request["json"]
        assert set(payload) == {"model", "input_audio", "provider"}
        assert payload["model"] == speech.DEFAULT_MODEL
        assert payload["provider"] == {"zdr": True, "data_collection": "deny"}
        assert payload["input_audio"]["format"] == "mp3"
        assert "synthetic-key" not in json.dumps(payload)
    assert base64.b64decode(session.requests[0][1]["json"]["input_audio"]["data"]) == b"mp3-bytes"
    await client.close()
    await client.close()
    assert session.closed
    with pytest.raises(speech.SpeechError) as caught:
        await client.transcribe(b"audio")
    assert caught.value.reason is speech.SpeechErrorReason.CLOSED
    assert len(session.requests) == 2


async def test_configured_model_is_sent_without_a_language_hint(transport):
    session, _ = transport
    session.responses = [Response({"text": "hello"})]
    client = speech.SpeechClient("synthetic-key", model="openai/whisper-large-v3-turbo")
    try:
        await client.transcribe(b"audio")
    finally:
        await client.close()
    payload = session.requests[0][1]["json"]
    assert payload["model"] == "openai/whisper-large-v3-turbo" and "language" not in payload


@pytest.mark.parametrize(
    "options",
    [{"model": "Qwen/ASR"}, {"model": "no-slash"}, {"model": "a/b c"}, {"timeout_seconds": 0}],
)
def test_invalid_configuration_is_rejected(options):
    with pytest.raises(ValueError):
        speech.SpeechClient("synthetic-key", **options)


@pytest.mark.parametrize("key", ["", "  ", "line\nbreak", None])
def test_missing_or_unsafe_key_is_unavailable(key):
    with pytest.raises(speech.SpeechError) as caught:
        speech.SpeechClient(key)
    assert caught.value.reason is speech.SpeechErrorReason.UNAVAILABLE


@pytest.mark.parametrize(
    ("audio", "audio_format"), [(b"", "mp3"), ("text", "mp3"), (b"x" * (speech.MAX_AUDIO_BYTES + 1), "mp3"), (b"x", "../")]
)
async def test_invalid_audio_never_reaches_the_provider(transport, audio, audio_format):
    session, factory = transport
    client = speech.SpeechClient("synthetic-key")
    with pytest.raises(speech.SpeechError) as caught:
        await client.transcribe(audio, audio_format)
    assert caught.value.reason is speech.SpeechErrorReason.INVALID_REQUEST
    factory.assert_not_called()
    assert session.requests == []


@pytest.mark.parametrize(
    "payload",
    [
        {"error": {"message": "private provider text"}},
        {"text": None},
        {"text": 42},
        {"text": "ok", "usage": {"seconds": -1}},
        {"text": "ok", "usage": {"cost": "free"}},
        [],
        None,
    ],
)
async def test_invalid_protocol_is_a_sanitized_failure(transport, caplog, payload):
    session, _ = transport
    response = Response(payload)
    session.responses = [response]
    client = speech.SpeechClient("synthetic-key")
    try:
        with pytest.raises(speech.SpeechError) as caught:
            await client.transcribe(b"private audio")
    finally:
        await client.close()
    assert caught.value.reason is speech.SpeechErrorReason.INVALID_RESPONSE
    assert isinstance(caught.value, ExternalServiceError)
    assert "Не удалось распознать речь" in caught.value.text
    assert response.exited and len(session.requests) == 1
    diagnostic = str(caught.value) + repr(caught.value) + caplog.text
    assert "private provider text" not in diagnostic and "synthetic-key" not in diagnostic
    assert caught.value.__suppress_context__


@pytest.mark.parametrize("status", [301, 400, 401, 402, 429, 500])
async def test_http_failures_do_not_read_bodies_redirect_or_retry(transport, status):
    session, _ = transport
    response = Response(raw=b"private upstream error", status=status)
    session.responses = [response]
    client = speech.SpeechClient("synthetic-key")
    try:
        with pytest.raises(speech.SpeechError) as caught:
            await client.transcribe(b"audio")
    finally:
        await client.close()
    assert caught.value.reason is speech.SpeechErrorReason.UNAVAILABLE
    assert response.chunks_read == 0 and len(session.requests) == 1


async def test_compressed_and_oversized_responses_are_rejected(transport):
    session, _ = transport
    session.responses = [
        Response({"text": "ok"}, encoding="gzip"),
        Response({"text": "ok"}, content_length=speech.MAX_RESPONSE_BYTES + 1),
    ]
    client = speech.SpeechClient("synthetic-key")
    try:
        for _ in range(2):
            with pytest.raises(speech.SpeechError) as caught:
                await client.transcribe(b"audio")
            assert caught.value.reason is speech.SpeechErrorReason.INVALID_RESPONSE
    finally:
        await client.close()


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        (aiohttp.ClientConnectionError("private detail"), speech.SpeechErrorReason.UNAVAILABLE),
        (OSError("private detail"), speech.SpeechErrorReason.UNAVAILABLE),
        (asyncio.TimeoutError(), speech.SpeechErrorReason.TIMEOUT),
    ],
)
async def test_transport_failures_are_categorized(transport, failure, reason):
    session, _ = transport
    session.responses = [failure]
    client = speech.SpeechClient("synthetic-key")
    try:
        with pytest.raises(speech.SpeechError) as caught:
            await client.transcribe(b"audio")
    finally:
        await client.close()
    assert caught.value.reason is reason
    assert "private detail" not in repr(caught.value)


async def test_close_never_creates_an_unused_session(transport):
    session, factory = transport
    await speech.SpeechClient("synthetic-key").close()
    factory.assert_not_called()
