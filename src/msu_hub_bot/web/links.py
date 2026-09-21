"""User-bound launch context bridges group messages to private Mini App buttons."""

import base64
import hashlib
import hmac
import re
import struct
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlencode, urlsplit

from aiogram.types import InlineKeyboardButton, Message, WebAppInfo

LAUNCH_SECONDS = 2 * 60 * 60


def feedback_report_id(argument: str | None) -> str | None:
    """A review link identifies a report; the authenticated API grants access."""
    match = re.fullmatch(r"feedback_([0-9a-f]{16})", argument or "")
    return match[1] if match else None


class LaunchError(ValueError):
    def __init__(self) -> None:
        super().__init__("Ссылка устарела. Открой приложение из исходного чата ещё раз.")


@dataclass(frozen=True)
class Destination:
    chat_id: int
    thread_id: int | None = None


class WebAppLinks:
    def __init__(self, token: str, url: str) -> None:
        self.url = url.rstrip("/")
        endpoint = urlsplit(self.url)
        if self.url and (
            endpoint.scheme != "https"
            or not endpoint.hostname
            or endpoint.username
            or endpoint.password
            or endpoint.query
            or endpoint.fragment
        ):
            raise ValueError("Mini App URL must be an HTTPS address without credentials, query or fragment")
        self._key = hmac.digest(token.encode(), b"msu-hub:web-launch:v1", "sha256")
        self.username = ""

    def launch(self, user_id: int, destination: Destination, *, now: datetime) -> str:
        if user_id <= 0 or not destination.chat_id or (destination.thread_id or 0) < 0 or now.tzinfo is None:
            raise ValueError("Invalid Mini App destination")
        body = struct.pack(">qqI", destination.chat_id, destination.thread_id or 0, int(now.timestamp()) + LAUNCH_SECONDS)
        signature = hmac.digest(self._key, str(user_id).encode() + b":" + body, "sha256")[:12]
        return base64.urlsafe_b64encode(body + signature).decode().rstrip("=")

    def destination(self, user_id: int, launch: str | None, *, now: datetime) -> Destination:
        if user_id <= 0 or now.tzinfo is None:
            raise ValueError("Invalid Mini App identity")
        if not launch:
            return Destination(user_id)
        try:
            if not re.fullmatch(r"[A-Za-z0-9_-]{43}", launch):
                raise ValueError
            decoded = base64.urlsafe_b64decode(launch + "=")
            body, supplied = decoded[:-12], decoded[-12:]
            expected = hmac.digest(self._key, str(user_id).encode() + b":" + body, "sha256")[:12]
            if not hmac.compare_digest(expected, supplied):
                raise ValueError
            chat_id, thread_id, expires = struct.unpack(">qqI", body)
            if not 0 <= expires - now.timestamp() <= LAUNCH_SECONDS or not chat_id or thread_id < 0:
                raise ValueError
            if chat_id > 0 and chat_id != user_id:
                raise ValueError
            return Destination(chat_id, thread_id or None)
        except ValueError, TypeError, struct.error:
            raise LaunchError() from None

    def button(self, message: Message, *, now: datetime, label: str = "Открыть приложение") -> InlineKeyboardButton | None:
        if not self.url or message.from_user is None or message.from_user.is_bot or message.sender_chat is not None:
            return None
        launch = self.launch(message.from_user.id, Destination(message.chat.id, message.message_thread_id), now=now)
        if message.chat.type == "private":
            return self.private_button(launch, label=label)
        if not self.username:
            return None
        return InlineKeyboardButton(text=label, url=f"https://t.me/{self.username}?start=app_{launch}")

    def private_button(self, launch: str, *, label: str = "Открыть приложение") -> InlineKeyboardButton:
        return InlineKeyboardButton(text=label, web_app=WebAppInfo(url=self.url + "/?" + urlencode({"launch": launch})))

    def feedback_button(self, report_id: str) -> InlineKeyboardButton | None:
        if feedback_report_id("feedback_" + report_id) is None:
            raise ValueError("Invalid feedback report ID")
        if not self.url or not self.username:
            return None
        return InlineKeyboardButton(text="Открыть отзыв", url=f"https://t.me/{self.username}?start=feedback_{report_id}")

    def private_feedback_button(self, report_id: str) -> InlineKeyboardButton:
        if feedback_report_id("feedback_" + report_id) is None:
            raise ValueError("Invalid feedback report ID")
        return InlineKeyboardButton(text="Открыть отзыв", web_app=WebAppInfo(url=self.url + "/?" + urlencode({"feedback": report_id})))

    @staticmethod
    def request_message_id(user_id: int, request_id: str) -> int:
        # Negative IDs keep browser actions separate from real Telegram messages.
        value = int.from_bytes(hashlib.blake2s(f"{user_id}:{request_id}".encode(), digest_size=8).digest(), "big")
        return -(value % (2**63 - 1) + 1)
