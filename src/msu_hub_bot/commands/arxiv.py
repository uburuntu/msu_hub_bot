from aiogram import html
import random
from textwrap import shorten
from typing import NotRequired, TypedDict, cast

import arxiv
from aiogram.types import Message
from aiogram.filters import CommandObject
from aiogram.utils.markdown import hbold, hcode, hitalic, hlink

from msu_hub_bot.execution.executor import TPExecutor
from msu_hub_bot.utils import one_liner


class Paper(TypedDict):
    arxiv_url: str
    title: str
    authors: list[str]
    summary: str
    pdf_url: NotRequired[str]


def arxiv_random() -> list[Paper]:
    return cast(list[Paper], arxiv.query(query="all:a", start=random.randint(0, 10_000), sort_by="lastUpdatedDate", max_results=5))


def arxiv_search(query: str) -> list[Paper]:
    return cast(list[Paper], arxiv.query(query=query, max_results=5))


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
