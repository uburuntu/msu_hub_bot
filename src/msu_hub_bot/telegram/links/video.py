"""Generic yt-dlp presentation with ordinary video emojis."""

from msu_hub_bot.providers.vk.utils import href
from msu_hub_bot.providers.ydl import YDL, Preview
from msu_hub_bot.telegram.constants import TELEGRAM_CAPTION_MAX_LEN
from msu_hub_bot.telegram.links.vk import utf16_length


def _shorten(title: str, limit: int) -> str:
    """Fit visible text into a UTF-16 budget without splitting a surrogate pair."""
    if utf16_length(title) <= limit:
        return title
    kept, size = [], 0
    for char in title:
        width = 2 if ord(char) > 0xFFFF else 1
        if size + width > limit - 1:
            break
        kept.append(char)
        size += width
    return "".join(kept).rstrip() + "…"


def text_with_preview(url: str) -> tuple[str, Preview | None] | None:
    # Mirrors and embedded links can resolve to YouTube through the generic route.
    result = YDL.extract(url, require_youtube_video=True)
    if result is None:
        return None
    title, links, preview = result
    prefix = "📺 " if preview else "🎞 "
    labels = ", ".join(label for _, label, _, _ in links)
    # Some extractors (e.g. Telegram posts) use the whole post as the title; captions count visible text only.
    title = _shorten(title, max(1, TELEGRAM_CAPTION_MAX_LEN - utf16_length(prefix + "\n\n— " + labels)))
    text = href(preview[0], "📺") + " " if preview else "🎞 "
    text += href(url, title) + "\n\n— "
    text += ", ".join(href(link, label) for link, label, _, _ in links)
    return text, preview
