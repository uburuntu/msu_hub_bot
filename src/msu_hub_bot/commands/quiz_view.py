"""Small, shared presentation primitives for message-based chat quizzes."""

from dataclasses import dataclass

from aiogram.types import MessageEntity
from aiogram.utils.formatting import Text, TextLink

CAPTION_LIMIT = 1024
PAGE_SIZE = 4


@dataclass(frozen=True)
class View:
    caption: str
    entities: list[MessageEntity]
    page: int
    pages: int


def compact(value: str, limit: int) -> str:
    """Flatten whitespace and shorten without splitting an emoji's UTF-16 pair."""
    if limit < 0:
        raise ValueError("Text limit must be non-negative")
    value = " ".join(value.split())
    if len(Text(value)) <= limit:
        return value
    remaining = max(0, limit - 1)
    prefix: list[str] = []
    for char in value:
        width = 2 if ord(char) > 0xFFFF else 1
        if width > remaining:
            break
        prefix.append(char)
        remaining -= width
    return "".join(prefix).rstrip() + ("…" if limit else "")


def _user_label_parts(name: str, username: str | None) -> tuple[str, str]:
    handle = compact(username.lstrip("@"), 32) if username else ""
    return compact(name, 48) or "Игрок", f" (@{handle})" if handle else ""


def user_label_size(name: str, username: str | None) -> int:
    """Measure a caption row without constructing off-page mention entities."""
    return len(Text(*_user_label_parts(name, username)))


def user_label(user_id: int, name: str, username: str | None) -> Text:
    label, handle = _user_label_parts(name, username)
    return Text(TextLink(label, url=f"tg://user?id={user_id}"), handle)


def user_label_plain(name: str, username: str | None) -> Text:
    """Build a plain-text user label without a Telegram user link.

    Args:
        name: The user's display name, compacted to fit caption limits.
        username: Optional Telegram username, rendered in parentheses.

    Returns:
        A Text object with name and optional (@username) handle, containing
        no clickable entity or user reference.
    """
    label, handle = _user_label_parts(name, username)
    return Text(label, handle)
