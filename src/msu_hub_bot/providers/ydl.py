"""Extract direct media links without downloading videos or runtime components."""

import time
from contextlib import nullcontext
from html import escape
from typing import Any, cast

import requests
from yt_dlp import YoutubeDL
from yt_dlp.utils import YoutubeDLError

from msu_hub_bot.utils import megabytes
from msu_hub_bot.providers.vk.utils import href

MediaLink = tuple[str, str, int | None, int | None]
Preview = tuple[str, int | None, int | None]


class _QuietLogger:
    """Extractor diagnostics may contain signed URLs and provider response bodies."""

    def debug(self, message: str) -> None:
        pass

    def warning(self, message: str) -> None:
        pass

    def error(self, message: str) -> None:
        pass


def _number(value: object) -> int:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def _media_link(data: dict[str, Any]) -> MediaLink | None:
    url = data.get("url")
    if not isinstance(url, str) or not url.startswith(("https://", "http://")):
        return None
    # Extractors do not consistently populate the human-readable format field.
    label = str(data.get("format") or "").partition(" - ")[2]
    label = label or str(data.get("format_note") or data.get("resolution") or data.get("format_id") or data.get("ext") or "Video")
    return url, label, _number(data.get("width")) or None, _number(data.get("height")) or None


class YDL:
    @classmethod
    def create_ydl(cls) -> YoutubeDL:
        return YoutubeDL(
            {
                "quiet": True,
                "logger": _QuietLogger(),
                "noplaylist": True,
                "geo_bypass": True,
                "cachedir": False,
                "socket_timeout": 10,
                "retries": 1,
                "extractor_retries": 1,
                "js_runtimes": {"deno": {}},
                "remote_components": [],
            }
        )

    @classmethod
    def extract_data(cls, url: str, ydl: YoutubeDL | None = None) -> dict[str, Any] | None:
        try:
            with nullcontext(ydl) if ydl is not None else cls.create_ydl() as client:
                info = client.extract_info(url, download=False)
                return cast(dict[str, Any], info) if isinstance(info, dict) else None
        except YoutubeDLError:
            return None

    @classmethod
    def extract(cls, real_url: str, ydl: YoutubeDL | None = None) -> tuple[str, list[MediaLink], Preview | None] | None:
        info = cls.extract_data(real_url, ydl)
        if not info:
            return None
        extractor = str(info.get("extractor", "")).lower()
        if extractor in ("generic", "yandexmusic") or info.get("_type") in ("playlist", "multi_video"):
            return None

        raw_formats = info.get("formats")
        formats = [item for item in raw_formats if isinstance(item, dict)] if isinstance(raw_formats, list) else [info]
        formats = [item for item in formats if "m3u8" not in str(item.get("protocol") or "")]
        if extractor == "youtube":
            # Telegram previews need sound; separate DASH video streams are links to silent video.
            formats = [item for item in formats if item.get("acodec") not in (None, "none")]
            audio = [item for item in formats if item.get("vcodec") == "none"]
            formats = [item for item in formats if item.get("vcodec") not in (None, "none")]
            if audio:
                formats.append(max(audio, key=lambda item: _number(item.get("asr"))))
        elif extractor == "vk":
            formats = [
                item
                for item in formats
                if str(item.get("format_id") or "").startswith(("url", "cache"))
                or item.get("format_id") in ("extra_data", "live_mp4", "postlive_mp4")
            ]
            formats.sort(key=lambda item: _number(item.get("height")), reverse=True)

        links = [link for item in formats if (link := _media_link(item)) is not None]
        if not links:
            return None
        links, preview = cls.post_process_links(links)
        return str(info.get("title") or "Video"), links, preview

    @classmethod
    def preview(cls, url: str) -> str | None:
        result = cls.extract(url)
        return result[2][0] if result and result[2] else None

    @classmethod
    def post_process_links(cls, links: list[MediaLink]) -> tuple[list[MediaLink], Preview | None]:
        # A rejected HEAD request must not discard otherwise useful download links.
        headers: dict[str, tuple[int, str]] = {}
        deadline = time.monotonic() + 20
        with requests.Session() as session:
            for url, *_ in links:
                if url in headers:
                    continue
                headers[url] = (0, "")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    continue
                try:
                    with session.head(url, timeout=min(5, remaining), allow_redirects=True) as response:
                        response.raise_for_status()
                        size = max(0, int(response.headers.get("Content-Length", 0)))
                        content_type = response.headers.get("Content-Type", "").partition(";")[0].strip().lower()
                        headers[url] = (size, content_type)
                except requests.RequestException, ValueError:
                    continue

        ordered = sorted(links, key=lambda link: headers[link[0]][0], reverse=True)
        preview = next(
            (
                (url, width, height)
                for url, _, width, height in ordered
                if 0 < headers[url][0] < megabytes(20) and headers[url][1] == "video/mp4"
            ),
            None,
        )
        return ordered, preview

    @classmethod
    def text_with_preview(cls, url: str) -> tuple[str, Preview | None] | None:
        result = cls.extract(url)
        if result is None:
            return None
        title, links, preview = result
        text = href(escape(preview[0], quote=True), "📺") + " " if preview else "🎞 "
        text += href(escape(url, quote=True), escape(title)) + "\n\n— "
        text += ", ".join(href(escape(link, quote=True), escape(label)) for link, label, _, _ in links)
        return text, preview
