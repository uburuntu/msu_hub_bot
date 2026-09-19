"""Escape raw VK text once and keep links/media within public HTTP boundaries."""

import ipaddress
import re
from html import escape
from urllib.parse import urlsplit

from msu_hub_bot.utils import shorten

_WIKI_OR_URL = re.compile(r"\[([^ |\n]+)\|([^\]\n]+)\]|https?://[^\s<>\[\]\"']+", re.U)
_HASHTAG = re.compile(r"(#\S+)@\S+", re.U)
_HTML_TOKEN = re.compile(r"</?a\b[^>]*>|&(?:#[0-9]+|#x[0-9a-fA-F]+|[a-zA-Z]+);|.", re.S)
_MEDIA_DOMAINS = ("userapi.com", "vkuserphoto.ru", "vk.com", "vk.ru", "okcdn.ru", "mycdn.me", "vk-cdn.net")


def safe_url(value: str, *, media: bool = False) -> str:
    if len(value) > 2048 or re.search(r"[\s<>\"'\x00-\x1f\x7f]", value):
        return ""
    try:
        url = urlsplit(value)
        host = (url.hostname or "").lower().rstrip(".")
        if url.scheme not in {"https", "http"} or not host or url.username or url.password or url.port not in {None, 80, 443}:
            return ""
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")) or "." not in host:
            return ""
        try:
            if not ipaddress.ip_address(host).is_global:
                return ""
        except ValueError:
            pass
        if media and not any(host == domain or host.endswith("." + domain) for domain in _MEDIA_DOMAINS):
            return ""
        return value
    except ValueError:
        return ""


def href(url: str, text: str | None = None, url_cut_width: int = 32) -> str:
    label = escape(text or shorten(url, width=url_cut_width))
    return f'<a href="{escape(valid, quote=True)}">{label}</a>' if (valid := safe_url(url)) else label


def prepare_vk_text(text: str) -> str:
    text = _HASHTAG.sub(r"\1", text)
    result, previous = [], 0
    for match in _WIKI_OR_URL.finditer(text):
        result.append(escape(text[previous : match.start()]))
        if target := match[1]:
            if re.fullmatch(r"[A-Za-z0-9_.-]+", target):
                target = "https://vk.com/" + target
            elif target.startswith(("vk.com/", "vk.ru/")):
                target = "https://" + target
            result.append(href(target, match[2]))
        else:
            url = match[0]
            result.append(href(url, shorten(url, width=80)) if len(url) > 80 else escape(url))
        previous = match.end()
    result.append(escape(text[previous:]))
    return "".join(result)


def utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def bounded_html(text: str, source: str) -> str:
    """Fit one Telegram text message without cutting an entity, tag or emoji."""
    limit = 3500
    if utf16_length(text) <= limit:
        return text
    suffix = "\n\n" + href(source, "Читать целиком в VK →")
    budget = limit - utf16_length(suffix) - len("</a>")
    result, size, in_link = [], 0, False
    for match in _HTML_TOKEN.finditer(text):
        token = match[0]
        size += utf16_length(token)
        if size > budget:
            break
        result.append(token)
        if token.startswith("<a "):
            in_link = True
        elif token == "</a>":
            in_link = False
    return "".join(result) + ("</a>" if in_link else "") + suffix
