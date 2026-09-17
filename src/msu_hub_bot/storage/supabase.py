"""Authenticated, bounded access to the bot's Supabase RPC contracts."""

from __future__ import annotations

import asyncio
import json
import math
import re
import time
from collections.abc import Callable
from datetime import datetime
from enum import StrEnum
from typing import TypeVar

import aiohttp
from pydantic import BaseModel, ConfigDict, Field, JsonValue, SecretStr, StrictInt, TypeAdapter, ValidationError
from yarl import URL

from msu_hub_bot.storage.models import (
    ArchivedUpdate,
    ChatObservation,
    ChatRecord,
    DirectoryCreate,
    DirectoryPatch,
    DirectoryRecord,
    MembershipObservation,
    MessageObservation,
    TopicObservation,
    UsageStats,
    UserObservation,
    VkPatch,
    VkSubscription,
)
from msu_hub_bot.settings import Settings
from msu_hub_bot.telemetry import Backend, Boundary, Outcome, Telemetry

_Result = TypeVar("_Result")
_Record = TypeVar("_Record", bound=BaseModel)
_JSON: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)
_SETTINGS = TypeAdapter(dict[str, JsonValue])


class RepositoryFailure(StrEnum):
    AUTH = "authentication"
    DENIED = "permission_denied"
    UNAVAILABLE = "unavailable"
    TIMEOUT = "timeout"
    INVALID_RESPONSE = "invalid_response"
    REJECTED = "request_rejected"
    CLOSED = "closed"


class RepositoryError(Exception):
    """Only local classifications and HTTP status may escape the transport."""

    def __init__(self, code: RepositoryFailure, status: int | None = None) -> None:
        self.code = code
        self.status = status
        super().__init__(f"Database operation failed: {code.value}" + (f" (HTTP {status})" if status is not None else ""))


class RepositoryUnavailable(RepositoryError):
    pass


class RepositoryAuthError(RepositoryError):
    pass


class RepositoryProtocolError(RepositoryError):
    def __init__(self) -> None:
        super().__init__(RepositoryFailure.INVALID_RESPONSE)


class _Token(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    access_token: SecretStr
    refresh_token: SecretStr
    expires_in: StrictInt = Field(gt=0)
    token_type: str


class _Health(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    schema_version: StrictInt
    bot_id: StrictInt = Field(gt=0, le=2**63 - 1)


def _record(model: type[_Record], value: JsonValue) -> _Record:
    try:
        return model.model_validate(value)
    except ValidationError:
        raise RepositoryProtocolError() from None


def _optional(model: type[_Record], value: JsonValue) -> _Record | None:
    return None if value is None else _record(model, value)


def _records(model: type[_Record], value: JsonValue) -> list[_Record]:
    if not isinstance(value, list):
        raise RepositoryProtocolError()
    return [_record(model, item) for item in value]


def _settings(value: JsonValue) -> dict[str, JsonValue]:
    try:
        return _SETTINGS.validate_python(value, strict=True)
    except ValidationError:
        raise RepositoryProtocolError() from None


def _boolean(value: JsonValue) -> bool:
    if not isinstance(value, bool):
        raise RepositoryProtocolError()
    return value


def _void(value: JsonValue) -> None:
    if value is not None:
        raise RepositoryProtocolError()


def _observation(
    value: ChatObservation | UserObservation | MembershipObservation | TopicObservation | MessageObservation,
) -> dict[str, JsonValue]:
    # Field presence distinguishes a sparse sighting from an explicit clear.
    payload: dict[str, JsonValue] = value.model_dump(mode="json", exclude_unset=True)
    payload["observed_at"] = value.observed_at.isoformat()
    return payload


def _reject_nonfinite(_: str) -> None:
    raise ValueError("Invalid JSON number")


def _finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("Invalid JSON number")
    return parsed


class SupabaseRepository:
    """Own one HTTP pool and one rotating Auth session; never retry an RPC write."""

    def __init__(
        self,
        config: Settings,
        *,
        telemetry: Telemetry | None = None,
        operation_timeout: float = 15,
        max_response_bytes: int = 8 * 1024 * 1024,
    ) -> None:
        try:
            url = URL(config.supabase_url)
            valid_url = (
                url.scheme in {"http", "https"}
                and bool(url.host)
                and url.user is None
                and not url.query_string
                and not url.fragment
                and url.path in {"", "/"}
            )
            bot_id = int(config.bot_token.partition(":")[0])
        except ValueError, TypeError:
            raise ValueError("Invalid Supabase repository configuration") from None
        if (
            not valid_url
            or not re.fullmatch(r"[a-z_][a-z0-9_]*", config.supabase_schema)
            or not all((config.supabase_key, config.supabase_email, config.supabase_password))
            or not 0 < bot_id < 2**63
            or not math.isfinite(operation_timeout)
            or operation_timeout <= 0
            or max_response_bytes < 1
        ):
            raise ValueError("Invalid Supabase repository configuration")
        self._url = str(url).rstrip("/")
        self._key = config.supabase_key
        self._email = config.supabase_email
        self._password = config.supabase_password
        self._schema = config.supabase_schema
        self._bot_id = bot_id
        self._timeout = operation_timeout
        self._max_response_bytes = max_response_bytes
        self._telemetry = telemetry or Telemetry()
        self._auth_lock = asyncio.Lock()
        self._token: _Token | None = None
        self._refresh_at = 0.0
        self._auth_retry_at = 0.0
        self._closed = False
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=operation_timeout),
            trust_env=False,
            cookie_jar=aiohttp.DummyCookieJar(),
        )

    async def close(self) -> None:
        self._closed = True
        self._token = None
        self._password = ""
        self._key = ""
        await self._session.close()

    async def _request(self, path: str, payload: dict[str, JsonValue], *, token: str | None = None) -> JsonValue:
        headers = {"apikey": self._key, "Accept": "application/json"}
        if token is not None:
            headers.update({"Authorization": f"Bearer {token}", "Content-Profile": self._schema, "Accept-Profile": self._schema})
        async with self._session.post(self._url + path, json=payload, headers=headers, allow_redirects=False) as response:
            if response.status in {401, 403}:
                if response.status == 401 and self._token is not None and self._token.access_token.get_secret_value() == token:
                    self._refresh_at = 0
                code = RepositoryFailure.AUTH if response.status == 401 else RepositoryFailure.DENIED
                raise RepositoryAuthError(code, response.status)
            if response.status == 429 or response.status >= 500:
                raise RepositoryUnavailable(RepositoryFailure.UNAVAILABLE, response.status)
            if not 200 <= response.status < 300:
                if token is None and 400 <= response.status < 500:
                    raise RepositoryAuthError(RepositoryFailure.AUTH, response.status)
                raise RepositoryError(RepositoryFailure.REJECTED, response.status)
            body = bytearray()
            async for chunk in response.content.iter_chunked(64 * 1024):
                body.extend(chunk)
                if len(body) > self._max_response_bytes:
                    raise RepositoryProtocolError()
            if response.status == 204 and not body:
                return None
            if response.content_type != "application/json":
                raise RepositoryProtocolError()
            try:
                return _JSON.validate_python(json.loads(body, parse_constant=_reject_nonfinite, parse_float=_finite_float), strict=True)
            except ValidationError, ValueError, RecursionError:
                raise RepositoryProtocolError() from None

    async def _access_token(self) -> str:
        async with self._auth_lock:
            if self._closed:
                raise RepositoryUnavailable(RepositoryFailure.CLOSED)
            if self._token is not None and time.monotonic() < self._refresh_at:
                return self._token.access_token.get_secret_value()
            if time.monotonic() < self._auth_retry_at:
                raise RepositoryUnavailable(RepositoryFailure.UNAVAILABLE)
            with self._telemetry.operation(Boundary.STORAGE, "database.auth", backend=Backend.SUPABASE, trace=False):
                previous = self._token
                # A lost refresh response may have rotated the token. A later
                # operation signs in afresh instead of replaying an uncertain refresh.
                self._token = None
                if previous is None:
                    grant = "password"
                    payload: dict[str, JsonValue] = {"email": self._email, "password": self._password}
                else:
                    grant = "refresh_token"
                    payload = {"refresh_token": previous.refresh_token.get_secret_value()}
                try:
                    value = await self._request(f"/auth/v1/token?grant_type={grant}", payload)
                    token = _record(_Token, value)
                    if (
                        token.token_type.lower() != "bearer"
                        or not token.access_token.get_secret_value()
                        or not token.refresh_token.get_secret_value()
                    ):
                        raise RepositoryProtocolError()
                except Exception:
                    # Waiting updates must not turn one failed refresh into a
                    # burst of password sign-ins against an unavailable server.
                    self._auth_retry_at = time.monotonic() + 5
                    raise
                if self._closed:
                    raise RepositoryUnavailable(RepositoryFailure.CLOSED)
                self._token = token
                self._refresh_at = time.monotonic() + token.expires_in - min(30, token.expires_in / 10)
                return token.access_token.get_secret_value()

    async def _rpc(
        self,
        function: str,
        payload: dict[str, JsonValue],
        decode: Callable[[JsonValue], _Result],
        *,
        operation: str = "database.read",
        trace: bool = True,
    ) -> _Result:
        with self._telemetry.operation(Boundary.STORAGE, operation, backend=Backend.SUPABASE, trace=trace) as span:
            try:
                async with asyncio.timeout(self._timeout):
                    token = await self._access_token()
                    return decode(await self._request(f"/rest/v1/rpc/{function}_v1", payload, token=token))
            except TimeoutError:
                span.set_outcome(Outcome.TIMEOUT)
                raise RepositoryUnavailable(RepositoryFailure.TIMEOUT) from None
            except aiohttp.ClientError, OSError:
                span.set_outcome(Outcome.UNAVAILABLE)
                raise RepositoryUnavailable(RepositoryFailure.UNAVAILABLE) from None
            except RepositoryError as error:
                if isinstance(error, RepositoryUnavailable):
                    span.set_outcome(Outcome.UNAVAILABLE)
                elif isinstance(error, RepositoryProtocolError):
                    span.set_outcome(Outcome.UNEXPECTED)
                else:
                    span.set_outcome(Outcome.REJECTED)
                raise
            except ValueError, TypeError, OverflowError:
                raise RepositoryProtocolError() from None

    async def check(self) -> None:
        def validate(value: JsonValue) -> None:
            result = _record(_Health, value)
            if result.schema_version != 1 or result.bot_id != self._bot_id:
                raise RepositoryProtocolError()

        await self._rpc("health", {}, validate, operation="database.check")

    async def ensure_chat(self, chat: ChatObservation) -> ChatRecord:
        return await self._rpc(
            "ensure_chat", {"p_chat": _observation(chat)}, lambda value: _record(ChatRecord, value), operation="database.write"
        )

    async def get_chat(self, chat_id: int) -> ChatRecord | None:
        return await self._rpc("get_chat", {"p_chat_id": chat_id}, lambda value: _optional(ChatRecord, value))

    async def load_settings(self, chat: ChatObservation) -> dict[str, JsonValue]:
        return await self._rpc("load_settings", {"p_chat": _observation(chat)}, _settings, trace=False)

    async def patch_settings(self, chat_id: int, changes: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return await self._rpc("patch_settings", {"p_chat_id": chat_id, "p_changes": changes}, _settings, operation="database.write")

    async def archive_update(self, update: ArchivedUpdate) -> None:
        payload: dict[str, JsonValue] = update.model_dump(mode="json")
        payload.update(
            users=[_observation(value) for value in update.users],
            chats=[_observation(value) for value in update.chats],
            memberships=[_observation(value) for value in update.memberships],
            topics=[_observation(value) for value in update.topics],
            messages=[_observation(value) for value in update.messages],
        )
        await self._rpc("archive_update", {"p_update": payload}, _void, operation="database.write", trace=False)

    async def statistics(self, since: datetime) -> UsageStats:
        if since.tzinfo is None or since.utcoffset() is None:
            raise ValueError("Statistics require a timezone-aware timestamp")
        return await self._rpc("statistics", {"p_since": since.isoformat()}, lambda value: _record(UsageStats, value))

    async def list_directory(self) -> list[DirectoryRecord]:
        # The RPC returns one JSONB array, not SETOF rows subject to a REST row cap.
        return await self._rpc("list_directory", {}, lambda value: _records(DirectoryRecord, value))

    async def get_directory(self, chat_id: int) -> DirectoryRecord | None:
        return await self._rpc("get_directory", {"p_chat_id": chat_id}, lambda value: _optional(DirectoryRecord, value))

    async def create_directory(self, entry: DirectoryCreate) -> DirectoryRecord:
        return await self._rpc(
            "create_directory",
            {"p_entry": entry.model_dump(mode="json")},
            lambda value: _record(DirectoryRecord, value),
            operation="database.write",
        )

    async def patch_directory(self, chat_id: int, changes: DirectoryPatch) -> DirectoryRecord | None:
        return await self._rpc(
            "patch_directory",
            {"p_chat_id": chat_id, "p_changes": changes.model_dump(mode="json", exclude_unset=True)},
            lambda value: _optional(DirectoryRecord, value),
            operation="database.write",
        )

    async def delete_directory(self, chat_id: int) -> bool:
        return await self._rpc("delete_directory", {"p_chat_id": chat_id}, _boolean, operation="database.write")

    async def list_vk_subscriptions(self) -> list[VkSubscription]:
        return await self._rpc("list_vk_subscriptions", {}, lambda value: _records(VkSubscription, value))

    async def upsert_vk_subscription(self, owner_id: int, chat_id: int, changes: VkPatch) -> VkSubscription:
        return await self._rpc(
            "upsert_vk_subscription",
            {"p_owner_id": owner_id, "p_chat_id": chat_id, "p_changes": changes.model_dump(mode="json", exclude_unset=True)},
            lambda value: _record(VkSubscription, value),
            operation="database.write",
        )

    async def advance_vk_cursor(self, owner_id: int, chat_id: int, last_post_id: int) -> None:
        await self._rpc(
            "advance_vk_cursor",
            {"p_owner_id": owner_id, "p_chat_id": chat_id, "p_last_post_id": last_post_id},
            _void,
            operation="database.write",
        )
