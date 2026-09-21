"""Anime metadata is bounded and cannot change a requested category or rating."""

import asyncio
import json
from types import SimpleNamespace

import aiohttp
import pytest

from msu_hub_bot.providers import tyan


class Response:
    def __init__(self, data=None, *, status=200, content_type="application/json", headers=None, body=None, wait=None):
        self.body = json.dumps(data).encode() if body is None else body
        self.status, self.content_type, self.headers = status, content_type, headers or {}
        self.request_info = SimpleNamespace(real_url="https://provider.example")
        self.content, self.wait, self.closed = self, wait, False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    async def iter_chunked(self, size):
        if self.wait is not None:
            self.wait[0].set()
            await self.wait[1].wait()
        for offset in range(0, len(self.body), size):
            yield self.body[offset : offset + size]


def transport(monkeypatch, responses):
    pending, sessions, calls = iter(responses), [], []

    class Session:
        def __init__(self, **kwargs):
            assert kwargs["timeout"].total == tyan.REQUEST_TIMEOUT
            assert kwargs["headers"]["User-Agent"] == tyan.USER_AGENT
            self.closed = False
            sessions.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            self.closed = True

        def get(self, url, *, allow_redirects):
            assert allow_redirects is False
            calls.append(url)
            response = next(pending)
            if isinstance(response, BaseException):
                raise response
            return response

    monkeypatch.setattr(tyan.aiohttp, "ClientSession", Session)
    return sessions, calls


def neko(category="neko"):
    extension = "png" if category in {"neko", "waifu"} else "gif"
    return {"results": [{"url": f"https://nekos.best/api/v2/{category}/synthetic.{extension}"}]}


@pytest.mark.parametrize("category", sorted(tyan.NEKOS_CATEGORIES))
async def test_each_supported_neko_category_requests_that_exact_category(monkeypatch, category):
    sessions, calls = transport(monkeypatch, [Response(neko(category))])
    result = await tyan.TyanProvider().image("sfw", category)
    assert calls == [f"https://nekos.best/api/v2/{category}"]
    assert result.animated == (category not in {"neko", "waifu"})
    assert all(session.closed for session in sessions)


@pytest.mark.parametrize("rating,category,format_", [("sfw", "lick", "gif"), ("nsfw", "neko", "img"), ("nsfw", "blowjob", "gif")])
async def test_purrbot_preserves_rating_category_and_media_kind(monkeypatch, rating, category, format_):
    extension = "gif" if format_ == "gif" else "jpg"
    url = f"https://cdn.purrbot.site/{rating}/{category}/{format_}/synthetic.{extension}"
    sessions, calls = transport(monkeypatch, [Response({"error": False, "link": url})])
    result = await tyan.TyanProvider().image(rating, category)
    assert result.url == url and result.animated == (format_ == "gif")
    assert calls == [f"https://api.purrbot.site/v2/img/{rating}/{category}/{format_}"]
    assert sessions[0].closed


@pytest.mark.parametrize(
    "response",
    [
        Response({"results": []}),
        Response({"results": [{"url": 42}]}),
        Response({"results": [{"url": "https://other.example/a.png"}]}),
        Response({"results": [{"url": "https://nekos.best/api/v2/waifu/wrong.png"}]}),
        Response({"results": [{"url": "https://nekos.best/api/v2/neko/../nsfw/wrong.png"}]}),
        Response({"results": [{"url": "http://nekos.best/api/v2/neko/wrong.png"}]}),
        Response({"results": [{"url": "https://user:password@nekos.best/api/v2/neko/wrong.png"}]}),
        Response({"results": [{"url": "https://nekos.best/api/v2/neko/wrong.gif"}]}),
        Response(content_type="text/html", body=b"provider HTML"),
        Response(body=b"not JSON"),
        Response(body=b" " * (tyan.MAX_METADATA_BYTES + 1)),
    ],
)
async def test_invalid_metadata_is_unavailable_without_retry_or_retaining_provider_body(monkeypatch, response):
    sessions, calls = transport(monkeypatch, [response])
    with pytest.raises(tyan.TyanUnavailable) as error:
        await tyan.TyanProvider().image("sfw", "neko")
    assert "provider HTML" not in str(error.value) and "password" not in str(error.value)
    assert len(calls) == 1 and response.closed and sessions[0].closed


async def test_purrbot_cannot_return_nsfw_media_for_sfw_category(monkeypatch):
    transport(monkeypatch, [Response({"error": False, "link": "https://cdn.purrbot.site/nsfw/lick/gif/wrong.gif"})])
    with pytest.raises(tyan.TyanUnavailable):
        await tyan.TyanProvider().image("sfw", "lick")


@pytest.mark.parametrize("failure", [aiohttp.ClientConnectionError("Synthetic disconnect"), Response(status=503)])
async def test_transient_get_retries_once_then_returns_one_result(monkeypatch, failure):
    sessions, calls = transport(monkeypatch, [failure, Response(neko())])
    assert (await tyan.TyanProvider().image("sfw", "neko")).url.endswith("synthetic.png")
    assert len(calls) == 2 and len(sessions) == 1 and sessions[0].closed


async def test_dns_failure_is_not_retried_and_concurrent_callers_share_cooldown(monkeypatch):
    failure = aiohttp.ClientConnectorDNSError(
        SimpleNamespace(host="provider.example", port=443, ssl=True), OSError("Synthetic DNS failure")
    )
    sessions, calls = transport(monkeypatch, [failure])
    provider = tyan.TyanProvider()
    errors = await asyncio.gather(*(provider.image("sfw", "neko") for _ in range(3)), return_exceptions=True)
    assert all(isinstance(error, tyan.TyanUnavailable) for error in errors)
    assert len(calls) == len(sessions) == 1 and sessions[0].closed


async def test_rate_limit_cooldown_is_bounded_and_other_provider_stays_available(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(tyan, "time", SimpleNamespace(monotonic=lambda: now[0]))
    sessions, calls = transport(
        monkeypatch,
        [
            Response(status=429, headers={"Retry-After": "90"}),
            Response({"error": False, "link": "https://cdn.purrbot.site/sfw/lick/gif/synthetic.gif"}),
            Response(neko()),
        ],
    )
    provider = tyan.TyanProvider()
    with pytest.raises(tyan.TyanUnavailable):
        await provider.image("sfw", "neko")
    now[0] += 31
    with pytest.raises(tyan.TyanUnavailable):
        await provider.image("sfw", "neko")
    assert len(calls) == 1
    assert (await provider.image("sfw", "lick")).animated
    now[0] += 60
    assert not (await provider.image("sfw", "neko")).animated
    assert len(calls) == 3 and all(session.closed for session in sessions)


@pytest.mark.parametrize("cancel", [False, True])
async def test_deadline_and_cancellation_close_response_session_and_release_capacity(monkeypatch, cancel):
    entered, release = asyncio.Event(), asyncio.Event()
    response = Response(neko(), wait=(entered, release))
    sessions, _ = transport(monkeypatch, [response, Response(neko())])
    monkeypatch.setattr(tyan, "TOTAL_TIMEOUT", 0.03 if not cancel else 1)
    provider = tyan.TyanProvider()
    task = asyncio.create_task(provider.image("sfw", "neko"))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        if cancel:
            task.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else tyan.TyanUnavailable):
            await asyncio.wait_for(task, 1)
        assert response.closed and sessions[0].closed
        assert not provider._locks["nekos_best"].locked()
        if cancel:
            assert not (await provider.image("sfw", "neko")).animated
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("rating,category", [("sfw", "shinobu"), ("nsfw", "waifu"), ("nsfw", "trap"), ("invalid", "neko")])
async def test_unavailable_categories_never_request_a_random_fallback(monkeypatch, rating, category):
    sessions, calls = transport(monkeypatch, [])
    assert not tyan.category_available(rating, category)
    with pytest.raises(tyan.TyanUnavailable):
        await tyan.TyanProvider().image(rating, category)
    assert sessions == calls == []
