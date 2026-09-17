"""Retained provider contracts use real SDK parsing with synthetic transport."""

import asyncio
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import arxiv
import mcstatus.server
import pytest
import requests
from mcstatus import JavaServer
from mcstatus.responses.java import JavaStatusResponse

from msu_hub_bot.commands import arxiv as papers
from msu_hub_bot.commands.minecraft import MinecraftStatus


def atom_feed(count: int = 5, offset: int = 0) -> bytes:
    entries = []
    for index in range(count):
        pdf = f'<link title="pdf" href="https://arxiv.org/pdf/synthetic-{index}" />' if index != 1 else ""
        entries.append(
            f"<entry><id>https://arxiv.org/abs/synthetic-{index}</id>"
            "<updated>2026-01-01T00:00:00Z</updated><published>2025-12-01T00:00:00Z</published>"
            f"<title>Paper {index} &amp; friends</title><summary>Synthetic summary {index}.</summary>"
            "<author><name>First Author</name></author><author><name>Second Author</name></author>"
            f"{pdf}</entry>"
        )
    return (
        '<feed xmlns="http://www.w3.org/2005/Atom" xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">'
        f"<opensearch:totalResults>{offset + count}</opensearch:totalResults>"
        f"<opensearch:startIndex>{offset}</opensearch:startIndex>"
        f"<opensearch:itemsPerPage>{count}</opensearch:itemsPerPage>" + "".join(entries) + "</feed>"
    ).encode()


@dataclass
class ArxivTransport:
    body: bytes = field(default_factory=atom_feed)
    calls: list[tuple[requests.PreparedRequest, dict[str, Any]]] = field(default_factory=list)
    closed: int = 0
    status: int = 200
    error: Exception | None = None


@pytest.fixture
def arxiv_transport(monkeypatch: pytest.MonkeyPatch) -> ArxivTransport:
    transport = ArxivTransport()
    original_close = papers._ArxivSession.close

    def send(adapter: requests.adapters.HTTPAdapter, request: requests.PreparedRequest, **kwargs: Any) -> requests.Response:
        transport.calls.append((request, kwargs))
        if transport.error:
            raise transport.error
        response = requests.Response()
        response.status_code = transport.status
        response._content = transport.body
        response.url = request.url or ""
        return response

    def close(session: papers._ArxivSession) -> None:
        transport.closed += 1
        original_close(session)

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    monkeypatch.setattr(papers._ArxivSession, "close", close)
    return transport


def test_arxiv_search_parses_atom_into_the_existing_paper_contract(arxiv_transport: ArxivTransport) -> None:
    result = papers.arxiv_search("all:friends & cats")

    assert len(result) == 5
    assert result[0] == {
        "arxiv_url": "https://arxiv.org/abs/synthetic-0",
        "title": "Paper 0 & friends",
        "authors": ["First Author", "Second Author"],
        "summary": "Synthetic summary 0.",
        "pdf_url": "https://arxiv.org/pdf/synthetic-0",
    }
    assert "pdf_url" not in result[1]
    assert len(arxiv_transport.calls) == 1 and arxiv_transport.closed == 1
    request, options = arxiv_transport.calls[0]
    assert parse_qs(urlsplit(request.url or "").query) == {
        "search_query": ["all:friends & cats"],
        "sortBy": ["relevance"],
        "sortOrder": ["descending"],
        "start": ["0"],
        "max_results": ["5"],
    }
    assert options["timeout"] == papers.REQUEST_TIMEOUT


@pytest.mark.parametrize("offset", [0, 7, 10_000])
def test_arxiv_random_returns_five_papers_after_the_offset(
    offset: int, arxiv_transport: ArxivTransport, monkeypatch: pytest.MonkeyPatch
) -> None:
    def randint(low: int, high: int) -> int:
        assert (low, high) == (0, 10_000)
        return offset

    monkeypatch.setattr(papers.random, "randint", randint)
    arxiv_transport.body = atom_feed(offset=offset)

    assert len(papers.arxiv_random()) == 5
    assert len(arxiv_transport.calls) == 1 and arxiv_transport.closed == 1
    request, options = arxiv_transport.calls[0]
    query = parse_qs(urlsplit(request.url or "").query)
    assert query["search_query"] == ["all:a"]
    assert query["start"] == [str(offset)] and query["max_results"] == ["5"]
    assert query["sortBy"] == ["lastUpdatedDate"] and query["sortOrder"] == ["descending"]
    assert options["timeout"] == papers.REQUEST_TIMEOUT


def test_arxiv_empty_feed_closes_the_session(arxiv_transport: ArxivTransport) -> None:
    arxiv_transport.body = atom_feed(count=0)
    assert papers.arxiv_search("synthetic") == []
    assert arxiv_transport.closed == 1


def test_arxiv_read_timeout_is_bounded_and_closes_the_session(arxiv_transport: ArxivTransport) -> None:
    error = requests.ReadTimeout("synthetic")
    arxiv_transport.error = error
    with pytest.raises(requests.ReadTimeout) as caught:
        papers.arxiv_search("synthetic")
    assert caught.value is error
    assert len(arxiv_transport.calls) == 1 and arxiv_transport.closed == 1
    assert arxiv_transport.calls[0][1]["timeout"] == papers.REQUEST_TIMEOUT


def test_arxiv_http_failures_retry_once_and_close(arxiv_transport: ArxivTransport) -> None:
    arxiv_transport.status = 503
    with pytest.raises(arxiv.HTTPError):
        papers.arxiv_search("synthetic")
    assert len(arxiv_transport.calls) == 2 and arxiv_transport.closed == 1
    assert all(options["timeout"] == papers.REQUEST_TIMEOUT for _, options in arxiv_transport.calls)


async def test_minecraft_async_lookup_preserves_real_status_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    lookup = AsyncMock(return_value=mcstatus.server.Address("synthetic.invalid", 25566))
    status = JavaStatusResponse.build(
        {
            "players": {"online": 1, "max": 20, "sample": [{"name": "Synthetic", "id": "00000000-0000-0000-0000-000000000001"}]},
            "version": {"name": "Synthetic 1.21", "protocol": 767},
            "description": "Synthetic",
        },
        latency=12.4,
    )
    request_status = AsyncMock(return_value=status)
    monkeypatch.setattr(mcstatus.server, "async_minecraft_srv_address_lookup", lookup)
    monkeypatch.setattr(JavaServer, "async_status", request_status)

    text = await MinecraftStatus.mc_status.__wrapped__(MinecraftStatus, "synthetic.invalid")
    lookup.assert_awaited_once_with("synthetic.invalid", default_port=25565, lifetime=3)
    request_status.assert_awaited_once_with()
    assert "(1 / 20)" in text and "— Synthetic" in text
    assert "Synthetic 1.21" in text and "12 мс" in text


@pytest.mark.parametrize("failure", [TimeoutError, asyncio.CancelledError])
async def test_minecraft_async_dns_preserves_offline_and_cancellation(
    failure: type[BaseException], monkeypatch: pytest.MonkeyPatch
) -> None:
    lookup = AsyncMock(side_effect=failure)
    status = AsyncMock()
    monkeypatch.setattr(JavaServer, "async_lookup", lookup)
    monkeypatch.setattr(JavaServer, "async_status", status)

    if failure is asyncio.CancelledError:
        with pytest.raises(asyncio.CancelledError):
            await MinecraftStatus.mc_status.__wrapped__(MinecraftStatus, "synthetic.invalid")
    else:
        text = await MinecraftStatus.mc_status.__wrapped__(MinecraftStatus, "synthetic.invalid")
        assert "оффлайн" in text
    lookup.assert_awaited_once_with("synthetic.invalid")
    status.assert_not_awaited()
