"""Public provider fallbacks validate complete protocols and bound every body."""

import asyncio
import io
import json
from unittest.mock import AsyncMock

import pytest

from msu_hub_bot.providers import http, knowledge, pdf
from msu_hub_bot.providers.exceptions import BadRequestError


class Response:
    def __init__(self, payload, *, status=200, length=None):
        self.body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.status = status
        self.content_length = length
        self.content = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def iter_chunked(self, size):
        for start in range(0, len(self.body), size):
            yield self.body[start : start + size]


class Session:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return next(self.responses)

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return next(self.responses)


def install(monkeypatch, responses):
    session = Session(responses)

    def factory(**kwargs):
        return session

    monkeypatch.setattr(pdf.aiohttp, "ClientSession", factory)
    return session


def pdf_responses(*, output=b"%PDF-synthetic", state="3"):
    return [
        Response(b'pdf24.workerServers = [{"host":"filetools0.pdf24.org"}];'),
        Response({"available": True}),
        Response([{"file": "input", "name": "synthetic.txt"}]),
        Response({"jobId": "synthetic"}),
        Response({"status": "done", "job": {"0.state": state}}),
        Response(output),
    ]


async def test_pdf_complete_protocol_keeps_cookie_session_and_delivers_bytes(monkeypatch):
    session = install(monkeypatch, pdf_responses())
    source = io.BytesIO(b"Synthetic text")
    with await pdf.convert_to_pdf(source, "../../синтетический.txt", "text/plain") as result:
        assert result.getvalue() == b"%PDF-synthetic" and result.name == "синтетический.pdf"
    assert session.closed and not source.closed
    assert all(call[2]["allow_redirects"] is False for call in session.calls)
    assert session.calls[-1][2]["params"] == {"action": "downloadJobResult", "jobId": "synthetic"}
    assert len({url for _, url, _ in session.calls[1:]}) == 1


@pytest.mark.parametrize("body", [b"<html>Login</html>", b'pdf24.workerServers = [{"host":"attacker.example"}];'])
async def test_pdf_rejects_changed_worker_manifest_before_upload(monkeypatch, body):
    session = install(monkeypatch, [Response(body)])
    with pytest.raises(BadRequestError):
        await pdf.convert_to_pdf(io.BytesIO(b"text"), "input.txt", "text/plain")
    assert len(session.calls) == 1 and session.closed


@pytest.mark.parametrize("state,output", [("4", b"%PDF-synthetic"), ("3", b"<html>Error</html>")])
async def test_pdf_rejects_failed_job_and_non_pdf_output(monkeypatch, state, output):
    session = install(monkeypatch, pdf_responses(state=state, output=output))
    with pytest.raises(BadRequestError):
        await pdf.convert_to_pdf(io.BytesIO(b"text"), "input.txt", "text/plain")
    assert session.closed


async def test_pdf_download_limit_is_enforced_on_actual_bytes(monkeypatch):
    monkeypatch.setattr(pdf, "MAX_PDF_BYTES", 8)
    install(monkeypatch, pdf_responses())
    with pytest.raises(BadRequestError):
        await pdf.convert_to_pdf(io.BytesIO(b"text"), "input.txt", "text/plain")


@pytest.mark.parametrize("cancel", [False, True])
async def test_pdf_pending_job_has_deadline_and_owned_session(monkeypatch, cancel):
    responses = pdf_responses()[:4] + [Response({"status": "pending"})]
    session = install(monkeypatch, responses)
    sleeping = asyncio.Event()

    async def pending(_):
        sleeping.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(pdf.asyncio, "sleep", pending)
    monkeypatch.setattr(pdf, "JOB_TIMEOUT_SECONDS", 0.02 if not cancel else 2)
    task = asyncio.create_task(pdf.convert_to_pdf(io.BytesIO(b"text"), "input.txt", "text/plain"))
    try:
        await asyncio.wait_for(sleeping.wait(), 1)
        if cancel:
            task.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else TimeoutError):
            await task
        assert session.closed
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("query,language", [("Moscow", "en"), ("Москва", "ru")])
async def test_unavailable_instant_answer_falls_back_to_sourced_wikipedia(monkeypatch, query, language):
    session = install(
        monkeypatch,
        [
            Response(b"blocked", status=403),
            Response(
                {
                    "query": {
                        "pages": [
                            {
                                "title": query,
                                "extract": "A sourced introduction",
                                "fullurl": f"https://{language}.wikipedia.org/wiki/Moscow",
                            }
                        ]
                    }
                }
            ),
        ],
    )
    result = await knowledge.answer(query)
    assert result["Heading"] == query and result["AbstractText"] == "A sourced introduction"
    assert result["AbstractURL"].startswith(f"https://{language}.wikipedia.org/")
    assert session.calls[1][2]["params"]["gsrsearch"] == query
    assert session.closed


async def test_valid_instant_answer_preserves_redirect_without_wikipedia(monkeypatch):
    session = install(monkeypatch, [Response({"Redirect": "https://example.org/search?q=x"})])
    assert (await knowledge.answer("!example x"))["Redirect"] == "https://example.org/search?q=x"
    assert len(session.calls) == 1


async def test_empty_instant_answer_uses_encyclopedia_and_empty_search_is_valid(monkeypatch):
    install(monkeypatch, [Response({}), Response({"batchcomplete": True})])
    assert not (await knowledge.answer("missing entry"))["AbstractText"]


@pytest.mark.parametrize("page", [None, {"title": "Name", "extract": "Text", "fullurl": "https://attacker.example/"}])
async def test_wikipedia_rejects_malformed_pages_and_foreign_sources(monkeypatch, page):
    install(monkeypatch, [Response({}), Response({"query": {"pages": [page]}})])
    with pytest.raises(BadRequestError):
        await knowledge.answer("query")


async def test_response_limits_check_declared_and_actual_body_sizes():
    for response in [Response(b"x", length=9), Response(b"123456789")]:
        with pytest.raises(BadRequestError):
            await http.read_limited(response, 8)


async def test_knowledge_cancellation_propagates(monkeypatch):
    session = install(monkeypatch, [])
    monkeypatch.setattr(knowledge, "_instant_answer", AsyncMock(side_effect=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        await knowledge.answer("query")
    assert session.closed


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "utf-16", "cp1251"])
def test_plain_text_pdf_wrapper_preserves_cyrillic_without_interpreting_markup(encoding):
    text = 'Привет <img src="https://example.org/private"> & мир\nНовая строка'
    payload, filename, mime_type = pdf._upload_payload(text.encode(encoding), "текст.txt", "text/plain")
    document = payload.decode()
    assert "Привет" in document and "Новая строка" in document
    assert "<img " not in document and "&lt;img" in document
    assert filename == "текст.html" and mime_type == "text/html"


def test_office_document_bytes_are_never_reinterpreted_as_text():
    data = b"PK\xff\x00synthetic"
    assert pdf._upload_payload(data, "file.docx", "application/octet-stream") == (data, "file.docx", "application/octet-stream")
