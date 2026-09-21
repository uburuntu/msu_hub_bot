"""Public TikTok posts, with complete photo albums and bounded video downloads."""

import json
import re
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field

from msu_hub_bot.providers.link_download import download_image, download_video, request_page
from msu_hub_bot.providers.link_diagnostics import LinkReason, LinkStage, record_link_diagnostic
from msu_hub_bot.providers.link_models import LinkAsset, LinkPost

_PAGE_HOSTS = ("tiktok.com", "www.tiktok.com", "vm.tiktok.com", "vt.tiktok.com")
_IMAGE_HOSTS = (".tiktokcdn.com", ".tiktokcdn-us.com", ".tiktokcdn-eu.com", ".byteoversea.com", ".ibytedtos.com")
_POST = re.compile(r"/@(?P<handle>[A-Za-z0-9._]{0,64})/(?P<kind>video|photo)/(?P<id>[1-9][0-9]{0,19})/?")
_SHORT = re.compile(r"/[A-Za-z0-9_-]{1,64}/?")
_HANDLE = re.compile(r"[A-Za-z0-9._]{1,64}")
_TIMEOUT = 40
_MAX_PAGE_BYTES = 2 * 1024 * 1024
_MAX_MEDIA_BYTES = 100 * 1024 * 1024
_MAX_PHOTO_BYTES = 9 * 1024 * 1024
_MAX_PHOTOS = 50
_MAX_VIDEO_DURATION = 180


@dataclass(frozen=True, slots=True)
class _PostRef:
    id: str
    handle: str
    kind: str

    @property
    def page_url(self) -> str:
        # Photo pages can be an empty shell; this route exposes either post type.
        return f"https://www.tiktok.com/@{self.handle or '_'}/video/{self.id}"


def normalize_tiktok_url(url: str) -> str | None:
    """Admit individual posts and short links, without tracking or host ambiguity."""
    if not url or len(url) > 16_384 or "\\" in url or any(ord(char) <= 32 or ord(char) == 127 for char in url):
        return None
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        if parsed.scheme not in {"http", "https"} or host not in _PAGE_HOSTS or parsed.netloc.lower() != host:
            return None
        path = parsed.path
        if host in {"vm.tiktok.com", "vt.tiktok.com"}:
            if not _SHORT.fullmatch(path):
                return None
        elif path.startswith("/t/"):
            if not _SHORT.fullmatch(path[2:]):
                return None
            host = "www.tiktok.com"
        elif _POST.fullmatch(path):
            host = "www.tiktok.com"
        else:
            return None
        return urlunsplit(("https", host, path.rstrip("/"), "", ""))
    except ValueError:
        return None


def _reference(url: str) -> _PostRef | None:
    parsed = urlsplit(url)
    match = _POST.fullmatch(parsed.path) if parsed.hostname == "www.tiktok.com" else None
    return _PostRef(match["id"], match["handle"], match["kind"]) if match else None


class _Payload(BaseModel):
    model_config = ConfigDict(extra="ignore")


class _Author(_Payload):
    nickname: str = ""
    uniqueId: str = ""
    privateAccount: bool = False


class _ImageURLs(_Payload):
    urlList: list[str] = Field(min_length=1, max_length=8)


class _Photo(_Payload):
    imageURL: _ImageURLs


class _Album(_Payload):
    title: str = ""
    images: list[_Photo] = Field(min_length=1, max_length=_MAX_PHOTOS)


class _Content(_Payload):
    desc: str = ""


class _Video(_Payload):
    duration: float = Field(default=0, ge=0, allow_inf_nan=False)


class _Item(_Payload):
    id: str
    author: _Author
    desc: str = ""
    contents: list[_Content] = Field(default_factory=list, max_length=1000)
    imagePost: _Album | None = None
    video: _Video | None = None
    privateItem: bool = False
    secret: bool = False
    forFriend: bool = False
    isContentClassified: bool = False


class _Hydration(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.matches = 0
        self.active = False
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script" and dict(attrs).get("id") == "__UNIVERSAL_DATA_FOR_REHYDRATION__":
            self.matches += 1
            self.active = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self.active = False

    def handle_data(self, data: str) -> None:
        if self.active:
            self.parts.append(data)


def _item(body: bytes, post_id: str) -> _Item | None:
    if len(body) > _MAX_PAGE_BYTES:
        record_link_diagnostic(LinkStage.ADAPTER, LinkReason.TOO_LARGE)
        return None
    try:
        parser = _Hydration()
        parser.feed(body.decode("utf-8"))
        parser.close()
        if parser.matches != 1:
            record_link_diagnostic(LinkStage.ADAPTER, LinkReason.MISSING_HYDRATION if not parser.matches else LinkReason.SCHEMA_MISMATCH)
            return None
        data = json.loads("".join(parser.parts))
        for key in ("__DEFAULT_SCOPE__", "webapp.video-detail"):
            if not isinstance(data, dict):
                record_link_diagnostic(LinkStage.ADAPTER, LinkReason.SCHEMA_MISMATCH)
                return None
            data = data.get(key)
        if not isinstance(data, dict) or type(data.get("statusCode")) is not int:
            record_link_diagnostic(LinkStage.ADAPTER, LinkReason.SCHEMA_MISMATCH)
            return None
        if data["statusCode"] != 0:
            record_link_diagnostic(LinkStage.ADAPTER, LinkReason.UNAVAILABLE)
            return None
        info = data.get("itemInfo")
        if not isinstance(info, dict):
            record_link_diagnostic(LinkStage.ADAPTER, LinkReason.SCHEMA_MISMATCH)
            return None
        item = _Item.model_validate(info.get("itemStruct"))
        if item.id != post_id:
            record_link_diagnostic(LinkStage.ADAPTER, LinkReason.ID_MISMATCH)
            return None
        if any((item.privateItem, item.secret, item.forFriend, item.isContentClassified, item.author.privateAccount)):
            record_link_diagnostic(LinkStage.ADAPTER, LinkReason.PRIVATE)
            return None
        return item
    except ValueError, RecursionError:
        record_link_diagnostic(LinkStage.ADAPTER, LinkReason.SCHEMA_MISMATCH)
        return None


def _photos(album: _Album, page_url: str, deadline: float) -> tuple[LinkAsset, ...] | None:
    assets: list[LinkAsset] = []
    remaining = _MAX_MEDIA_BYTES
    for photo in album.images:
        asset = None
        for url in dict.fromkeys(photo.imageURL.urlList[:2]):
            if time.monotonic() >= deadline or remaining <= 0:
                return None
            asset = download_image(
                url,
                deadline=deadline,
                allowed_hosts=_IMAGE_HOSTS,
                referer=page_url,
                max_bytes=min(_MAX_PHOTO_BYTES, remaining),
            )
            if asset is not None:
                break
        if asset is None or asset.kind != "photo" or not asset.data or len(asset.data) > remaining:
            return None
        assets.append(asset)
        remaining -= len(asset.data)
    return tuple(assets)


def fetch_tiktok(url: str) -> LinkPost | None:
    """Return a complete public post, or quietly leave the original link alone."""
    normalized = normalize_tiktok_url(url)
    if normalized is None:
        record_link_diagnostic(LinkStage.ADAPTER, LinkReason.UNSUPPORTED)
        return None
    deadline = time.monotonic() + _TIMEOUT
    ref = _reference(normalized)
    if ref is None:
        resolved = request_page(normalized, deadline=deadline, allowed_hosts=_PAGE_HOSTS, max_bytes=_MAX_PAGE_BYTES)
        if resolved is None or (canonical := normalize_tiktok_url(resolved[0])) is None:
            record_link_diagnostic(LinkStage.ADAPTER, LinkReason.UNAVAILABLE if resolved is None else LinkReason.UNSUPPORTED)
            return None
        ref = _reference(canonical)
        if ref is None:
            record_link_diagnostic(LinkStage.ADAPTER, LinkReason.UNSUPPORTED)
            return None
    if time.monotonic() >= deadline:
        record_link_diagnostic(LinkStage.ADAPTER, LinkReason.TIMEOUT)
        return None
    response = request_page(ref.page_url, deadline=deadline, allowed_hosts=_PAGE_HOSTS, max_bytes=_MAX_PAGE_BYTES)
    if response is None or (final_url := normalize_tiktok_url(response[0])) is None:
        record_link_diagnostic(LinkStage.ADAPTER, LinkReason.UNAVAILABLE if response is None else LinkReason.UNSUPPORTED)
        return None
    final_ref = _reference(final_url)
    if final_ref is None or final_ref.id != ref.id:
        record_link_diagnostic(LinkStage.ADAPTER, LinkReason.ID_MISMATCH if final_ref is not None else LinkReason.SCHEMA_MISMATCH)
        return None
    if (item := _item(response[1], ref.id)) is None:
        return None
    if item.imagePost is not None:
        assets = _photos(item.imagePost, ref.page_url, deadline)
        kind = "photo"
    else:
        if item.video is None or item.video.duration > _MAX_VIDEO_DURATION:
            record_link_diagnostic(LinkStage.ADAPTER, LinkReason.POLICY)
            return None
        if time.monotonic() >= deadline:
            record_link_diagnostic(LinkStage.ADAPTER, LinkReason.TIMEOUT)
            return None
        video = download_video(ref.page_url, deadline=deadline, max_duration=_MAX_VIDEO_DURATION)
        assets = (video,) if video is not None and video.kind == "video" and video.data else None
        kind = "video"
    if not assets:
        record_link_diagnostic(LinkStage.ADAPTER, LinkReason.UNAVAILABLE)
        return None
    if time.monotonic() >= deadline:
        record_link_diagnostic(LinkStage.ADAPTER, LinkReason.TIMEOUT)
        return None
    username = item.author.uniqueId if _HANDLE.fullmatch(item.author.uniqueId) else None
    text = "\n\n".join(content.desc for content in item.contents if content.desc) or item.desc
    return LinkPost(
        site="tiktok",
        url=f"https://www.tiktok.com/@{username or ref.handle}/{kind}/{ref.id}",
        author=item.author.nickname or username or "TikTok",
        username=username,
        author_url=f"https://www.tiktok.com/@{username}" if username else None,
        title=item.imagePost.title if item.imagePost is not None else "",
        text=text,
        assets=assets,
    )
