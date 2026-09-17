from aiogram import html
import random
from textwrap import shorten
from typing import Any, NotRequired, TypedDict

import arxiv
import requests
from aiogram.types import Message
from aiogram.filters import CommandObject
from aiogram.utils.markdown import hbold, hcode, hitalic, hlink

from msu_hub_bot.execution.executor import TPExecutor
from msu_hub_bot.utils import one_liner

PAPER_LIMIT = 5
REQUEST_TIMEOUT = (5.0, 20.0)


class _ArxivSession(requests.Session):
    def get(self, url: str | bytes, **kwargs: Any) -> requests.Response:
        kwargs["timeout"] = REQUEST_TIMEOUT
        return super().get(url, **kwargs)


class Paper(TypedDict):
    arxiv_url: str
    title: str
    authors: list[str]
    summary: str
    pdf_url: NotRequired[str]


def _papers(query: str, *, offset: int = 0, sort_by: arxiv.SortCriterion = arxiv.SortCriterion.Relevance) -> list[Paper]:
    search = arxiv.Search(
        query=query,
        # Client.results subtracts the offset from this limit, not just from the feed.
        max_results=offset + PAPER_LIMIT,
        sort_by=sort_by,
        sort_order=arxiv.SortOrder.Descending,
    )
    client = arxiv.Client(page_size=PAPER_LIMIT, num_retries=1)
    # The SDK exposes neither session injection nor a timeout/close API.
    # Replace only this client's unused session; the context owns every request.
    client._session.close()
    with _ArxivSession() as session:
        client._session = session
        papers = []
        for result in client.results(search, offset=offset):
            paper: Paper = {
                "arxiv_url": result.entry_id,
                "title": result.title,
                "authors": [author.name for author in result.authors],
                "summary": result.summary,
            }
            if result.pdf_url:
                paper["pdf_url"] = result.pdf_url
            papers.append(paper)
        return papers


def arxiv_random() -> list[Paper]:
    return _papers("all:a", offset=random.randint(0, 10_000), sort_by=arxiv.SortCriterion.LastUpdatedDate)


def arxiv_search(query: str) -> list[Paper]:
    return _papers(query)


async def process_arxiv(message: Message, command: CommandObject, cpu_executor: TPExecutor) -> Message:
    help_link = hlink("q", "https://arxiv.org/help/api/user-manual#Appendices")

    def paper_info(paper: Paper) -> str:
        url = paper["arxiv_url"]
        title = html.quote(one_liner(paper["title"]))

        authors = ""
        for count, author in enumerate(paper["authors"]):
            if count > 1:
                authors += " et al."
                break
            authors += (", " if authors else "") + html.quote(one_liner(author))

        summary = html.quote(shorten(one_liner(paper["summary"]), width=300))
        pdf = ""
        if "pdf_url" in paper:
            pdf = " (" + hlink(".pdf", paper["pdf_url"]) + ")"
        return f"• {hbold(authors)}, {hlink(title, url)}{pdf}: {hitalic(summary)}"

    if query := command.args:
        header = hbold("📑 Search results") + f" ({help_link}):\n\n"
        search, timeouted = await cpu_executor.run(arxiv_search, query)
    else:
        header = hbold("🗞 Random papers") + ":\n\n"
        search, timeouted = await cpu_executor.run(arxiv_random)

    if timeouted:
        return await message.reply(hcode("🤷🏻‍♂️ Timeout"))
    if not search:
        return await message.reply(hcode("🤷🏻‍♂️ По запросу ничего не найдено") + f" ({help_link})")

    result = header + "\n\n".join(paper_info(paper) for paper in search)
    return await message.reply(result)
