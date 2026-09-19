"""Short sourced answers with a public encyclopedia fallback."""

import asyncio
import re
from typing import TypedDict
from urllib.parse import urlsplit

import aiohttp

from msu_hub_bot.providers.exceptions import BadRequestError
from msu_hub_bot.providers.http import USER_AGENT, read_json


class Answer(TypedDict):
    Redirect: str
    Heading: str
    AbstractText: str
    AbstractURL: str
    Image: str


def _empty() -> Answer:
    return Answer(Redirect="", Heading="", AbstractText="", AbstractURL="", Image="")


def _web_url(value: object) -> str:
    if not isinstance(value, str):
        return ""
    parsed = urlsplit(value)
    return value if parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username else ""


async def _instant_answer(session: aiohttp.ClientSession, query: str) -> Answer:
    async with session.get(
        "https://api.duckduckgo.com/",
        params={"q": query, "format": "json", "no_redirect": 1},
        allow_redirects=False,
        timeout=aiohttp.ClientTimeout(total=8),
    ) as response:
        result = await read_json(response)
    if not isinstance(result, dict):
        raise BadRequestError()
    if not all(isinstance(result.get(key, ""), str) for key in ("Heading", "AbstractText", "Redirect", "AbstractURL", "Image")):
        raise BadRequestError()
    return Answer(
        Redirect=_web_url(result.get("Redirect")),
        Heading=result.get("Heading", ""),
        AbstractText=result.get("AbstractText", ""),
        AbstractURL=_web_url(result.get("AbstractURL")),
        Image=result.get("Image", ""),
    )


async def _wikipedia(session: aiohttp.ClientSession, query: str) -> Answer:
    language = "ru" if re.search("[а-яё]", query, re.IGNORECASE) else "en"
    async with session.get(
        f"https://{language}.wikipedia.org/w/api.php",
        params={
            "action": "query",
            "generator": "search",
            "gsrsearch": query,
            "gsrlimit": 1,
            "prop": "extracts|info",
            "exintro": 1,
            "explaintext": 1,
            "exchars": 2200,
            "inprop": "url",
            "format": "json",
            "formatversion": 2,
        },
        allow_redirects=False,
    ) as response:
        result = await read_json(response)
    if not isinstance(result, dict) or result.get("error"):
        raise BadRequestError()
    section = result.get("query")
    if section is None:
        return _empty()
    if not isinstance(section, dict) or not isinstance(section.get("pages"), list):
        raise BadRequestError()
    for page in section["pages"]:
        if not isinstance(page, dict) or not all(isinstance(page.get(key), str) for key in ("title", "extract", "fullurl")):
            raise BadRequestError()
        url = _web_url(page["fullurl"])
        if urlsplit(url).hostname != f"{language}.wikipedia.org":
            raise BadRequestError()
        if page["extract"].strip():
            return Answer(Redirect="", Heading=page["title"], AbstractText=page["extract"], AbstractURL=url, Image="")
    return _empty()


async def answer(query: str) -> Answer:
    try:
        async with (
            asyncio.timeout(25),
            aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}, timeout=aiohttp.ClientTimeout(total=15, connect=5)) as session,
        ):
            try:
                instant = await _instant_answer(session, query)
                if instant["Redirect"] or instant["AbstractText"]:
                    return instant
            except aiohttp.ClientError, BadRequestError, TimeoutError:
                pass
            return await _wikipedia(session, query)
    except aiohttp.ClientError, TimeoutError, ValueError:
        raise BadRequestError() from None
