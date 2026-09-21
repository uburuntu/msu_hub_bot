"""Environment configuration with the legacy attribute interface.

Credential fields are excluded from representations and selected for diagnostic
redaction. Optional providers are checked when used; imports need no credentials.
"""

import json
import os
import re
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def load_runtime_environment() -> None:
    """The deployment envelope preserves dollar signs, quotes, and newlines."""
    payload = os.environ.get("HUB_CONFIG_JSON")
    if not payload:
        return
    try:
        values = json.loads(payload)
        if not isinstance(values, dict):
            raise ValueError
        for key, value in values.items():
            if (
                not (re.fullmatch(r"HUB_[A-Z0-9_]+", key) or key == "LOGFIRE_TOKEN")
                or key == "HUB_CONFIG_JSON"
                or not isinstance(value, str)
                or "\0" in value
            ):
                raise ValueError
        for key, value in values.items():
            os.environ.setdefault(key, value)
    except TypeError, ValueError:
        raise ValueError("Invalid HUB_CONFIG_JSON deployment configuration") from None


load_runtime_environment()


class MissingIntegration(Exception):
    def __init__(self, *fields: str) -> None:
        self.fields = fields
        super().__init__("Optional integration is not configured")


class Settings(BaseSettings):
    name: str = "hub"
    bot_token: str = Field(default="", repr=False)
    storage_backend: Literal["supabase"] = "supabase"
    supabase_url: str = ""
    supabase_key: str = Field(default="", repr=False)
    supabase_email: str = ""
    supabase_password: str = Field(default="", repr=False)
    supabase_schema: str = "msu_hub_api"
    storage_contract: Literal["feature-state-v1"] = "feature-state-v1"
    proxy: str = Field(default="", repr=False)
    cert: str = ""
    pkey: str = Field(default="", repr=False)
    logs_file: str = ".local/logs/{name}.log"
    membership_inbox_path: Path = Path(".local/runtime/membership-inbox.sqlite3")
    health_check_url: str = Field(default="", repr=False)
    web_app_url: str = ""
    web_port: int = Field(default=8081, ge=1, le=65535)
    openrouter_api_key: str = Field(default="", repr=False)
    jev_enabled: bool = False
    jev_confidence: float = Field(default=0.8, ge=0.5, le=1)
    dumps_chat_id: int = 0
    events_chat_id: int = 0
    error_chat_id: int = 0
    owner_id: int = 0
    founder_ids: list[int] = Field(default_factory=list)
    forward_chat_ids: tuple[int, int] = (0, 0)
    related_chat_ids: tuple[int, int] = (0, 0)
    pookie_chat_id: int = 0
    echo_chat_id: int = 0
    excluded_chat_id: int = 0
    dvach_chat_ids: list[int] = Field(default_factory=lambda: [0] * 5, min_length=5, max_length=5)
    antibot_user_id: int = 0
    posting_tb_chat_id: int = 0
    posting_main_chat_id: int = 0
    vk_default_chat_id: int = 0
    tenet_sticker_owner_id: int = 0
    vk_user_token: str = Field(default="", repr=False)
    wolfram_token: str = Field(default="", repr=False)
    wit_tokens: list[str] = Field(default_factory=list, repr=False)
    jdoodle_tokens: list[tuple[str, str]] = Field(default_factory=list, repr=False)
    lingvanex_authorization: str = Field(default="", repr=False)
    lingvanex_image_authorization: str = Field(default="", repr=False)
    imgur_authorization: str = Field(default="", repr=False)
    owm_key: str = Field(default="", repr=False)
    owm_map_key: str = Field(default="", repr=False)
    mapbox_key: str = Field(default="", repr=False)
    acrcloud_host: str = ""
    acrcloud_access_key: str = Field(default="", repr=False)
    acrcloud_access_secret: str = Field(default="", repr=False)
    supporters_base: str = ""
    supporters_table: str = ""
    supporters_api_key: str = Field(default="", repr=False)

    model_config = SettingsConfigDict(env_prefix="HUB_", case_sensitive=False, extra="forbid", hide_input_in_errors=True)

    def validate_core(self) -> None:
        database_fields = ("supabase_url", "supabase_key", "supabase_email", "supabase_password")
        missing = [name for name in ("bot_token", *database_fields) if not getattr(self, name)]
        if missing:
            raise ValueError("Missing required settings: " + ", ".join("HUB_" + name.upper() for name in missing))
        if self.jev_enabled and not self.openrouter_api_key.strip():
            raise ValueError("HUB_OPENROUTER_API_KEY is required when HUB_JEV_ENABLED is true")
        endpoint = urlsplit(self.supabase_url)
        if (
            endpoint.scheme not in {"http", "https"}
            or not endpoint.hostname
            or endpoint.username is not None
            or endpoint.password is not None
            or endpoint.query
            or endpoint.fragment
            or endpoint.path not in {"", "/"}
        ):
            raise ValueError("Invalid HUB_SUPABASE_URL")
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", self.supabase_schema):
            raise ValueError("Invalid HUB_SUPABASE_SCHEMA")
        if self.web_app_url:
            web = urlsplit(self.web_app_url)
            if (
                web.scheme != "https"
                or not web.hostname
                or web.username is not None
                or web.password is not None
                or web.query
                or web.fragment
                or web.path not in {"", "/"}
            ):
                raise ValueError("Invalid HUB_WEB_APP_URL: expected an HTTPS origin")

    def require(self, *names: str) -> Any:
        missing = [name for name in names if not getattr(self, name)]
        if missing:
            raise MissingIntegration(*missing)
        return getattr(self, names[0]) if len(names) == 1 else tuple(getattr(self, name) for name in names)


settings = Settings()
