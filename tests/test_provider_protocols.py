"""Exercise provider response contracts without credentials or network access."""

import asyncio
import io
import json
from types import SimpleNamespace

import aiohttp
import pytest

from common.externals import other, topdf, urbandictionary
from common.externals.exceptions import BadRequestError, ExternalServiceError

from hub_bot.utils import wit


class Response:
    def __init__(self, payload=None, *, status=200, text=None, invalid_json=False):
        self.payload = payload
        self.status = status
        self.reason = "Synthetic status"
        self.body = text
        self.invalid_json = invalid_json

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def read(self):
        return (self.body if self.body is not None else json.dumps(self.payload)).encode()

    async def text(self):
        return (await self.read()).decode()

    async def json(self):
        if self.invalid_json:
            raise aiohttp.ContentTypeError(SimpleNamespace(real_url="https://example.org"), (), message="HTML response")
        return self.payload


class Session:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return next(self.responses)

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)


def session_for(monkeypatch, module, responses):
    session = Session(responses)
    monkeypatch.setattr(module.aiohttp, "ClientSession", lambda **kwargs: session)
    return session


async def test_gpt2_sends_json_and_returns_continuation(monkeypatch):
    session = session_for(monkeypatch, other, [Response({"replies": ["first", "Continuation"]})])
    assert await other.porfirevich("Prompt <text>") == "Continuation"
    assert session.calls[0][2]["json"] == {"prompt": "Prompt <text>", "length": 60}
    assert "data" not in session.calls[0][2]
    assert session.closed


@pytest.mark.parametrize("payload", [{}, {"replies": []}, {"replies": [" "]}, {"replies": [None]}, ["unexpected"]])
async def test_gpt2_bad_response_is_safe_failure(monkeypatch, payload):
    session_for(monkeypatch, other, [Response(payload)])
    with pytest.raises(BadRequestError):
        await other.porfirevich("Prompt")


@pytest.mark.parametrize("query,endpoint", [("a & b", "define"), ("", "random")])
async def test_urban_json_api_preserves_fields_and_query(monkeypatch, query, endpoint):
    session = session_for(
        monkeypatch,
        urbandictionary,
        [Response({"list": [{"word": " Synthetic ", "definition": " A\r\nB ", "example": " C\rD ", "thumbs_up": 12, "thumbs_down": 3}]})],
    )
    assert await urbandictionary.urban_dictionary(query) == [
        {"header": "Synthetic", "meaning": "A\nB", "example": "C\nD", "up": 12, "down": 3}
    ]
    assert session.calls[0][1].endswith("/" + endpoint)
    assert session.calls[0][2]["params"] == ({"term": query} if query else None)
    assert session.closed


@pytest.mark.parametrize("payload", [{}, {"list": {}}, {"list": [{}]}, {"list": [None]}])
async def test_urban_malformed_response_is_safe_failure(monkeypatch, payload):
    session_for(monkeypatch, urbandictionary, [Response(payload)])
    with pytest.raises(BadRequestError):
        await urbandictionary.urban_dictionary("synthetic")


async def test_urban_empty_result_is_valid(monkeypatch):
    session_for(monkeypatch, urbandictionary, [Response({"list": []})])
    assert await urbandictionary.urban_dictionary("synthetic") == []


@pytest.mark.parametrize(
    "payload",
    [
        {"status": False, "message": "private provider detail"},
        {"preview_size_output_image": None},
        {"preview_size_output_image": "//other.example/file"},
    ],
)
async def test_background_error_response_is_safe(monkeypatch, payload):
    session = session_for(monkeypatch, other, [Response(text='<meta name="csrf-token" content="synthetic">'), Response(payload)])
    with pytest.raises(BadRequestError) as error:
        await other.remove_bg(io.BytesIO(b"synthetic image"))
    assert "private provider detail" not in str(error.value)
    assert session.closed


async def test_background_missing_csrf_fails_before_upload(monkeypatch):
    session = session_for(monkeypatch, other, [Response(text="<html>Unavailable</html>")])
    with pytest.raises(BadRequestError):
        await other.remove_bg(io.BytesIO(b"image"))
    assert len(session.calls) == 1


async def test_background_preview_still_returns(monkeypatch):
    session_for(
        monkeypatch,
        other,
        [Response(text='<meta name="csrf-token" content="synthetic">'), Response({"preview_size_output_image": "/synthetic.png"})],
    )
    assert await other.remove_bg(io.BytesIO(b"image")) == "https://slazzer.com/synthetic.png"


async def test_background_api_failure_has_no_raw_output(monkeypatch, capsys):
    monkeypatch.setattr(other.settings, "remove_bg_api_key", "synthetic-key")
    session_for(monkeypatch, other, [Response({"error": "private provider detail"}, status=402)])
    with pytest.raises(BadRequestError):
        await other.remove_bg_api(io.BytesIO(b"image"))
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "status,payload",
    [
        (403, {"data": {"error": "private provider detail"}}),
        (200, {"success": False, "data": {}}),
        (200, {"data": {"processing": "unexpected"}}),
    ],
)
async def test_upload_failure_does_not_return_provider_error(monkeypatch, status, payload):
    monkeypatch.setattr(other.settings, "imgur_authorization", "synthetic-key")
    session = session_for(monkeypatch, other, [Response(payload, status=status)])
    with pytest.raises(BadRequestError) as error:
        await other.imgur_upload(io.BytesIO(b"image"))
    assert "private provider detail" not in str(error.value)
    assert session.closed


async def test_upload_ready_result_still_returns(monkeypatch):
    monkeypatch.setattr(other.settings, "imgur_authorization", "synthetic-key")
    image = {"link": "https://example.org/synthetic.png", "width": 100, "height": 100, "size": 50, "processing": None}
    session_for(monkeypatch, other, [Response({"data": image})])
    assert await other.imgur_upload(io.BytesIO(b"image")) == image


async def test_upload_polling_is_bounded_and_closes_session(monkeypatch):
    monkeypatch.setattr(other.settings, "imgur_authorization", "synthetic-key")
    monkeypatch.setattr(other, "UPLOAD_TIMEOUT_SECONDS", 0.01)
    session = session_for(monkeypatch, other, [Response({"data": {"id": "synthetic", "processing": {"status": "pending"}}})])
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(other.imgur_upload(io.BytesIO(b"image")), 1)
    assert session.closed


@pytest.mark.parametrize("response", [Response(status=301), Response(invalid_json=True), Response(["unexpected"])])
async def test_pdf_protocol_failure_is_safe_and_does_not_follow_upload_redirect(monkeypatch, response):
    session = session_for(monkeypatch, topdf, [response])
    with pytest.raises(BadRequestError):
        await topdf.convert_to_pdf(io.BytesIO(b"document"), "synthetic.png", "image/png")
    assert session.calls[0][2]["allow_redirects"] is False
    assert len(session.calls) == 1
    assert session.closed


@pytest.mark.parametrize("payload", [{"error": "private provider detail"}, {"text": None}, [], "unexpected"])
async def test_wit_http_200_error_is_not_silent_empty_text(payload):
    client = wit.WitAPI("synthetic-key")
    client.__dict__["session"] = Session([Response(payload)])
    with pytest.raises(wit.WitAPIError) as error:
        await client.speech(io.BytesIO(b"audio"))
    assert "private provider detail" not in repr(error.value)
    assert isinstance(error.value, ExternalServiceError)
    assert "Не удалось распознать речь" in error.value.text


async def test_wit_error_does_not_retain_response_body():
    client = wit.WitAPI("synthetic-key")
    client.__dict__["session"] = Session([Response(status=401, text="private provider detail")])
    with pytest.raises(wit.WitAPIError) as error:
        await client.speech(io.BytesIO(b"audio"))
    assert error.value.reason == "Synthetic status"


@pytest.mark.parametrize("response,expected", [(Response({"text": "Привет"}), "Привет"), (Response(status=400, text="no-body"), "")])
async def test_wit_success_and_empty_audio_keep_contract(response, expected):
    client = wit.WitAPI("synthetic-key")
    client.__dict__["session"] = Session([response])
    assert await client.speech(io.BytesIO(b"audio")) == expected
