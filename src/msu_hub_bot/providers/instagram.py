"""Public Instagram posts with complete captions and source-ordered media."""

import re
import time
from typing import Any, cast
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, ValidationError
from yt_dlp import YoutubeDL
from yt_dlp.extractor.instagram import InstagramIE

from msu_hub_bot.providers.link_download import allowed_url, download_image, download_video, extract_info, ydl_options
from msu_hub_bot.providers.link_diagnostics import LinkReason, LinkStage, record_link_diagnostic
from msu_hub_bot.providers.link_models import LinkAsset, LinkPost

_TIMEOUT = 75
_MAX_ASSETS = 20
_MAX_BYTES = 100 * 1024 * 1024
_CDN_HOSTS = (".cdninstagram.com", ".fbcdn.net")
_REFERER = "https://www.instagram.com/"
_POST_PATH = re.compile(r"/(?:[A-Za-z0-9_.]{1,30}/)?(?P<kind>p|reel|reels|tv)/(?P<id>[A-Za-z0-9_-]{5,64})/?")
_USERNAME = re.compile(r"[A-Za-z0-9_.]{1,30}")


def _post_url(url: str) -> tuple[str, str] | None:
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname not in {"instagram.com", "www.instagram.com"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
            or len(url) > 16_384
            or any(ord(char) < 33 or ord(char) == 127 for char in url)
        ):
            return None
        match = _POST_PATH.fullmatch(parsed.path)
        if match is None:
            return None
        kind = "reel" if match["kind"] == "reels" else match["kind"]
        return f"https://www.instagram.com/{kind}/{match['id']}/", match["id"]
    except ValueError:
        return None


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True, allow_inf_nan=False)


class _Variant(_Model):
    url: str
    width: int | None = Field(default=None, ge=0)
    height: int | None = Field(default=None, ge=0)
    acodec: str | None = None
    vcodec: str | None = None
    protocol: str | None = None
    manifest_url: str | None = None
    fragments: list[object] | None = None


class _Media(_Model):
    media_type: StrictInt = Field(alias="_ig_media_type", ge=1, le=2)
    has_audio: StrictBool | None = Field(default=None, alias="_ig_has_audio")
    thumbnails: list[_Variant] = Field(default_factory=list, max_length=100)
    formats: list[_Variant] = Field(default_factory=list, max_length=200)

    def source(self) -> str | None:
        candidates = self.thumbnails if self.media_type == 1 else self.formats
        usable = [
            (index, item)
            for index, item in enumerate(candidates)
            if allowed_url(item.url, _CDN_HOSTS)
            and (
                self.media_type == 1
                or (
                    item.protocol in {None, "http", "https"}
                    and item.manifest_url is None
                    and not item.fragments
                    and item.vcodec != "none"
                    and not any(codec in (item.vcodec or "").lower() for codec in ("hevc", "h265", "av1", "vp9"))
                    and (self.has_audio is False or item.acodec != "none")
                    and urlsplit(item.url).path.lower().endswith(".mp4")
                )
            )
        ]
        # Upstream thumbnails are ordered smaller to larger; preserve that tie-breaker when geometry is absent.
        best = max(usable, key=lambda value: ((value[1].width or 0) * (value[1].height or 0), value[0]), default=None)
        return best[1].url if best else None


class _Post(_Model):
    id: str
    media_type: StrictInt = Field(alias="_ig_media_type")
    count: StrictInt | None = Field(default=None, alias="_ig_count", ge=1, le=_MAX_ASSETS)
    description: str | None = None
    channel: str | None = None
    uploader: str | None = None
    entries: list[dict[str, Any]] | None = Field(default=None, max_length=_MAX_ASSETS)


def _parse_info(info: dict[str, Any], shortcode: str) -> tuple[_Post, list[_Media]] | None:
    try:
        post = _Post.model_validate(info)
        if post.id != shortcode:
            return None
        if post.media_type == 8:
            if not post.entries or (post.count is not None and post.count != len(post.entries)):
                return None
            items = [_Media.model_validate(entry) for entry in post.entries]
        elif post.media_type in {1, 2}:
            if post.entries or post.count not in {None, 1}:
                return None
            items = [_Media.model_validate(info)]
        else:
            return None
        return post, items
    except ValidationError:
        return None


def fetch_instagram(url: str) -> LinkPost | None:
    """Acquire one public post within a shared hard deadline; incomplete posts stay quiet."""
    parsed = _post_url(url)
    if parsed is None:
        record_link_diagnostic(LinkStage.ADAPTER, LinkReason.UNSUPPORTED)
        return None
    canonical, shortcode = parsed
    deadline = time.monotonic() + _TIMEOUT
    info = extract_info(canonical, deadline=deadline, provider="instagram")
    decoded = _parse_info(info, shortcode) if info else None
    if decoded is None:
        record_link_diagnostic(LinkStage.ADAPTER, LinkReason.INVALID_RESPONSE if info else LinkReason.UNAVAILABLE)
        return None
    post, items = decoded
    sources = [item.source() for item in items]
    if any(source is None for source in sources):
        record_link_diagnostic(LinkStage.ADAPTER, LinkReason.UNSUPPORTED)
        return None
    assets: list[LinkAsset] = []
    remaining = _MAX_BYTES
    for item, source in zip(items, sources, strict=True):
        expired = time.monotonic() >= deadline
        if expired or remaining <= 0 or source is None:
            record_link_diagnostic(LinkStage.ADAPTER, LinkReason.TIMEOUT if expired else LinkReason.TOO_LARGE)
            return None
        if item.media_type == 1:
            asset = download_image(
                source, deadline=deadline, allowed_hosts=_CDN_HOSTS, referer=_REFERER, max_bytes=min(remaining, 9 * 1024 * 1024)
            )
        else:
            asset = download_video(
                source,
                deadline=deadline,
                allowed_hosts=_CDN_HOSTS,
                referer=_REFERER,
                max_bytes=min(remaining, 49 * 1024 * 1024),
                require_audio=item.has_audio is True,
            )
        if asset is None or not asset.data or len(asset.data) > remaining or asset.kind != ("photo" if item.media_type == 1 else "video"):
            record_link_diagnostic(LinkStage.ADAPTER, LinkReason.UNAVAILABLE if asset is None else LinkReason.INVALID_RESPONSE)
            return None
        remaining -= len(asset.data)
        assets.append(asset)
    username = post.channel if post.channel and _USERNAME.fullmatch(post.channel) else None
    return LinkPost(
        site="instagram",
        url=canonical,
        author=post.uploader or username or "Instagram",
        username=username,
        author_url=f"https://www.instagram.com/{username}/" if username else None,
        text=post.description or "",
        assets=tuple(assets),
    )


class _NativeInstagramIE(InstagramIE):  # type: ignore[misc]  # Upstream has no typing metadata.
    """Keep source media identity; upstream owns its GraphQL, sessions and fallback parsing."""

    def _extract_product_media(self, product: dict[str, Any]) -> dict[str, Any]:
        result = cast(dict[str, Any], super()._extract_product_media(product))
        return {**result, "_ig_media_type": product.get("media_type"), "_ig_has_audio": product.get("has_audio")}

    def _extract_product(self, product_info: Any, video_id: str | None = None, get_comments: bool = True) -> dict[str, Any]:
        product = product_info[0] if isinstance(product_info, list) else product_info
        children = product.get("carousel_media")
        # Upstream filters malformed children; a native post must not silently lose an attachment.
        if isinstance(children, list) and any(not isinstance(child, dict) for child in children):
            raise ValueError("Incomplete Instagram media metadata")
        result = cast(dict[str, Any], super()._extract_product(product_info, video_id, get_comments=False))
        return {**result, "_ig_media_type": product.get("media_type"), "_ig_count": product.get("carousel_media_count")}


def _extract_instagram_info(url: str) -> dict[str, Any] | None:
    """Child-process entry: raw extraction keeps photos and single-post carousels."""
    if _post_url(url) is None:
        return None
    # Instagram's session and GraphQL requests are occasionally slow; one attempt gets a longer read window.
    options = {**ydl_options(), "ignore_no_formats_error": True, "socket_timeout": 15}
    with YoutubeDL(options) as client:
        info = _NativeInstagramIE(client).extract(url)
    if not isinstance(info, dict):
        return None
    keys = {"id", "description", "channel", "uploader", "entries", "_ig_media_type", "_ig_count", "_ig_has_audio", "thumbnails", "formats"}
    result = {key: value for key, value in info.items() if key in keys}
    if isinstance(result.get("entries"), list):
        result["entries"] = [
            {key: value for key, value in entry.items() if key in keys} if isinstance(entry, dict) else entry for entry in result["entries"]
        ]
    return result
