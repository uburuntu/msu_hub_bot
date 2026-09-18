"""Bounded, expiring Telegram authentication without browser-side credentials."""

import json
import re
from datetime import datetime
from urllib.parse import parse_qsl

from aiogram.utils.web_app import check_webapp_signature
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class AuthenticationError(ValueError):
    def __init__(self) -> None:
        super().__init__("Open the Mini App from Telegram again")


class WebUser(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, hide_input_in_errors=True)

    id: int = Field(strict=True, gt=0, lt=2**63)
    first_name: str = Field(max_length=256)
    last_name: str | None = Field(default=None, max_length=256)
    is_bot: bool = Field(default=False, strict=True)

    @property
    def name(self) -> str:
        name = " ".join(part for part in (self.first_name, self.last_name) if part)
        return name if len(name) <= 256 else name[:255] + "…"


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate field")
        result[key] = value
    return result


def authenticate(init_data: str, token: str, *, now: datetime) -> WebUser:
    try:
        if not init_data or len(init_data.encode()) > 16_384 or now.tzinfo is None:
            raise ValueError
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True, errors="strict", max_num_fields=32)
        fields = dict(pairs)
        if len(fields) != len(pairs) or not re.fullmatch(r"[0-9]{1,12}", fields.get("auth_date", "")):
            raise ValueError
        if not check_webapp_signature(token, init_data):
            raise ValueError
        age = now.timestamp() - int(fields["auth_date"])
        if not -30 <= age <= 3600:
            raise ValueError
        user = WebUser.model_validate(json.loads(fields["user"], object_pairs_hook=_unique_object))
        if user.is_bot:
            raise ValueError
        return user
    except KeyError, ValueError, TypeError, UnicodeError, ValidationError, RecursionError:
        raise AuthenticationError() from None
