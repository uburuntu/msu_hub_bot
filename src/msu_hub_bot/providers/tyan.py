"""Exact anime-image categories with bounded public metadata requests."""

import asyncio
import re
import time
from dataclasses import dataclass
from typing import Annotated, Literal
from urllib.parse import urlsplit

import aiohttp
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from msu_hub_bot.providers.exceptions import ExternalServiceError
from msu_hub_bot.telemetry import Boundary, Provider, Telemetry

REQUEST_TIMEOUT = 3
TOTAL_TIMEOUT = 8
MAX_METADATA_BYTES = 16 * 1024
COOLDOWN_SECONDS = 30
USER_AGENT = "MSUHubBot (https://t.me/msu_hub_bot)"

# Only exact category matches are admitted. Old buttons for unavailable
# categories remain meaningful errors instead of unrelated random pictures.
NEKOS_CATEGORIES = frozenset(
    "waifu neko cuddle cry hug kiss pat smug bonk yeet blush smile wave highfive handhold nom bite slap kick happy wink poke dance".split()
)
Backend = Literal["nekos_best", "purrbot"]
BoundedURL = Annotated[str, StringConstraints(min_length=1, max_length=2048)]
BoundedName = Annotated[str, StringConstraints(min_length=1, max_length=128)]


class TyanUnavailable(ExternalServiceError):
    def __init__(self) -> None:
        super().__init__("Сервис картинок временно недоступен. Попробуй чуть позже.")


class _Metadata(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True, hide_input_in_errors=True)


class _NekosImage(_Metadata):
    url: BoundedURL
    artist_name: BoundedName | None = None
    artist_href: BoundedURL | None = None
    source_url: BoundedURL | None = None
    anime_name: BoundedName | None = None


class _NekosResponse(_Metadata):
    results: Annotated[list[_NekosImage], Field(min_length=1, max_length=1)]


class _PurrbotResponse(_Metadata):
    error: Literal[False]
    link: BoundedURL


@dataclass(frozen=True)
class TyanImage:
    url: str
    animated: bool
    artist_name: str | None = None
    artist_url: str | None = None
    source_url: str | None = None
    anime_name: str | None = None


def _route(type_: str, category: str) -> tuple[Backend, str, bool] | None:
    if type_ == "sfw" and category in NEKOS_CATEGORIES:
        return "nekos_best", f"https://nekos.best/api/v2/{category}", category not in {"waifu", "neko"}
    if (type_, category) in {("sfw", "lick"), ("nsfw", "neko"), ("nsfw", "blowjob")}:
        animated = category != "neko"
        format_ = "gif" if animated else "img"
        return "purrbot", f"https://api.purrbot.site/v2/img/{type_}/{category}/{format_}", animated
    return None


def category_available(type_: str, category: str) -> bool:
    return _route(type_, category) is not None


def _https(value: str) -> str:
    url = urlsplit(value)
    if url.scheme != "https" or not url.hostname or url.username or url.password or url.port is not None or any(c.isspace() for c in value):
        raise ValueError("Invalid image metadata URL")
    return value


def _image(body: bytes, backend: Backend, type_: str, category: str, animated: bool) -> TyanImage:
    if backend == "nekos_best":
        result = _NekosResponse.model_validate_json(body).results[0]
        image = TyanImage(
            url=_https(result.url),
            animated=animated,
            artist_name=result.artist_name,
            artist_url=_https(result.artist_href) if result.artist_href else None,
            source_url=_https(result.source_url) if result.source_url else None,
            anime_name=result.anime_name,
        )
        host, prefix = "nekos.best", f"/api/v2/{category}/"
    else:
        result_purrbot = _PurrbotResponse.model_validate_json(body)
        image = TyanImage(url=_https(result_purrbot.link), animated=animated)
        host, prefix = "cdn.purrbot.site", f"/{type_}/{category}/{'gif' if animated else 'img'}/"
    url = urlsplit(image.url)
    suffixes = (".gif",) if animated else (".png", ".jpg", ".jpeg", ".webp")
    filename = url.path.removeprefix(prefix)
    if (
        url.hostname != host
        or not url.path.startswith(prefix)
        or re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z]+", filename) is None
        or not filename.lower().endswith(suffixes)
        or url.query
        or url.fragment
    ):
        raise ValueError("Unexpected image category or host")
    return image


class TyanProvider:
    def __init__(self) -> None:
        self._locks = {backend: asyncio.Lock() for backend in ("nekos_best", "purrbot")}
        self._cooldown_until: dict[Backend, float] = {}

    async def image(self, type_: str, category: str, *, telemetry: Telemetry | None = None) -> TyanImage:
        route = _route(type_, category)
        if route is None:
            raise TyanUnavailable()
        backend, endpoint, animated = route
        provider = Provider.NEKOS_BEST if backend == "nekos_best" else Provider.PURRBOT
        try:
            with (telemetry or Telemetry()).operation(Boundary.PROVIDER, "tyan.image", provider=provider):
                async with asyncio.timeout(TOTAL_TIMEOUT), self._locks[backend]:
                    if self._cooldown_until.get(backend, 0) > time.monotonic():
                        raise TyanUnavailable()
                    try:
                        async with aiohttp.ClientSession(
                            headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"},
                            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
                            auto_decompress=False,
                        ) as session:
                            for attempt in range(2):
                                try:
                                    return await self._request(session, endpoint, backend, type_, category, animated)
                                except (aiohttp.ClientError, TimeoutError) as error:
                                    transient = isinstance(error, (aiohttp.ClientConnectionError, aiohttp.ClientPayloadError, TimeoutError))
                                    if isinstance(error, (aiohttp.ClientConnectorDNSError, aiohttp.ClientSSLError)):
                                        transient = False
                                    if isinstance(error, aiohttp.ClientResponseError):
                                        transient = error.status in {500, 502, 503, 504}
                                    if attempt or not transient:
                                        raise
                                    await asyncio.sleep(0.2)
                    except aiohttp.ClientError, TimeoutError, TyanUnavailable:
                        self._cooldown_until[backend] = max(self._cooldown_until.get(backend, 0), time.monotonic() + COOLDOWN_SECONDS)
                        raise
        except aiohttp.ClientError, TimeoutError:
            raise TyanUnavailable() from None
        raise TyanUnavailable()

    async def _request(
        self, session: aiohttp.ClientSession, endpoint: str, backend: Backend, type_: str, category: str, animated: bool
    ) -> TyanImage:
        async with session.get(endpoint, allow_redirects=False) as response:
            if response.status == 429:
                # Avoid sleeping while a callback is active; future requests
                # honor a bounded server cooldown instead.
                retry = response.headers.get("Retry-After", "")
                seconds = min(300, max(COOLDOWN_SECONDS, int(retry))) if retry.isdecimal() and len(retry) <= 6 else COOLDOWN_SECONDS
                self._cooldown_until[backend] = time.monotonic() + seconds
            if response.status != 200:
                raise aiohttp.ClientResponseError(response.request_info, (), status=response.status)
            if response.content_type != "application/json":
                raise TyanUnavailable()
            body = bytearray()
            async for chunk in response.content.iter_chunked(4096):
                body.extend(chunk)
                if len(body) > MAX_METADATA_BYTES:
                    raise TyanUnavailable()
            try:
                return _image(bytes(body), backend, type_, category, animated)
            except ValidationError, ValueError:
                raise TyanUnavailable() from None
