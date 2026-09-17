"""Database-independent records and sparse observations of Telegram entities."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Self
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, model_validator

BigInt = Annotated[int, Field(strict=True, ge=-(2**63), lt=2**63)]


def utc_now() -> datetime:
    return datetime.now(UTC)


class DatabaseModel(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class StoredRecord(DatabaseModel):
    # SELECT responses may gain columns before an older application is replaced.
    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)
    id: UUID
    created: AwareDatetime


class UserObservation(DatabaseModel):
    user_id: BigInt
    is_bot: bool
    first_name: str
    last_name: str | None = None
    username: str | None = None
    language_code: str | None = None
    observed_at: AwareDatetime = Field(default_factory=utc_now)
    profile: dict[str, JsonValue] = Field(default_factory=dict)


class ChatObservation(DatabaseModel):
    chat_id: BigInt
    type: str
    title: str | None = None
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    observed_at: AwareDatetime = Field(default_factory=utc_now)
    profile: dict[str, JsonValue] = Field(default_factory=dict)


class UserRecord(StoredRecord):
    user_id: BigInt
    is_bot: bool
    first_name: str
    last_name: str | None = None
    username: str | None = None
    language_code: str | None = None
    metadata: JsonValue
    profile: dict[str, JsonValue] = Field(default_factory=dict)
    first_seen_at: AwareDatetime | None = None
    last_seen_at: AwareDatetime | None = None

    @property
    def full_name(self) -> str:
        return self.first_name if self.last_name is None else f"{self.first_name} {self.last_name}"


class ChatRecord(StoredRecord):
    chat_id: BigInt
    type: str
    title: str | None = None
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    metadata: JsonValue
    profile: dict[str, JsonValue] = Field(default_factory=dict)
    first_seen_at: AwareDatetime | None = None
    last_seen_at: AwareDatetime | None = None

    @property
    def full_name(self) -> str | None:
        if self.title is not None:
            return self.title
        if self.first_name is None:
            return None
        return self.first_name if self.last_name is None else f"{self.first_name} {self.last_name}"


class MembershipObservation(DatabaseModel):
    chat_id: BigInt
    user_id: BigInt
    observed_at: AwareDatetime = Field(default_factory=utc_now)
    status: str | None = None
    permissions: dict[str, JsonValue] = Field(default_factory=dict)


class TopicObservation(DatabaseModel):
    chat_id: BigInt
    thread_id: BigInt
    observed_at: AwareDatetime = Field(default_factory=utc_now)
    title: str | None = None
    is_closed: bool | None = None
    profile: dict[str, JsonValue] = Field(default_factory=dict)


class MessageObservation(DatabaseModel):
    chat_id: BigInt
    message_id: BigInt
    sent_at: AwareDatetime
    edited_at: AwareDatetime | None = None
    observed_at: AwareDatetime = Field(default_factory=utc_now)
    business_connection_id: str = ""
    sender_user_id: BigInt | None = None
    sender_chat_id: BigInt | None = None
    thread_id: BigInt | None = None
    reply_to_message_id: BigInt | None = None
    data: dict[str, JsonValue]


class ArchivedUpdate(DatabaseModel):
    id: UUID = Field(default_factory=uuid4)
    update_id: BigInt
    received_at: AwareDatetime = Field(default_factory=utc_now)
    kind: str
    handled: bool
    data: dict[str, JsonValue]
    # Only the retained backend can store bodies outside normalized message rows.
    legacy_data: dict[str, JsonValue] | None = Field(default=None, exclude=True, repr=False)
    users: list[UserObservation] = Field(default_factory=list)
    chats: list[ChatObservation] = Field(default_factory=list)
    memberships: list[MembershipObservation] = Field(default_factory=list)
    topics: list[TopicObservation] = Field(default_factory=list)
    messages: list[MessageObservation] = Field(default_factory=list)


class UsageStats(DatabaseModel):
    users: int
    chats: int
    updates: int
    handled_updates: int


class DirectoryCreate(DatabaseModel):
    chat_id: BigInt
    name: str
    section: str = "other"
    is_hidden: bool = False
    username_alias: str | None = ""
    members: BigInt | None = None
    pinned_message_id: BigInt | None = None


class DirectoryPatch(DatabaseModel):
    name: str | None = None
    section: str | None = None
    is_hidden: bool | None = None
    username_alias: str | None = None
    members: BigInt | None = None
    pinned_message_id: BigInt | None = None

    @model_validator(mode="after")
    def required_fields_cannot_be_cleared(self) -> Self:
        if any(name in self.model_fields_set and getattr(self, name) is None for name in ("name", "section", "is_hidden")):
            raise ValueError("Required directory fields cannot be cleared")
        return self


class DirectoryRecord(StoredRecord):
    chat_id: BigInt
    name: str
    section: str
    is_hidden: bool
    username_alias: str | None = None
    members: BigInt | None = None
    pinned_message_id: BigInt | None = None


class VkPatch(DatabaseModel):
    last_post_id: BigInt | None = None
    with_reposts: bool | None = None
    with_header: bool | None = None
    is_suspended: bool | None = None
    description: str | None = None

    @model_validator(mode="after")
    def required_fields_cannot_be_cleared(self) -> Self:
        names = ("last_post_id", "with_reposts", "with_header", "is_suspended")
        if any(name in self.model_fields_set and getattr(self, name) is None for name in names):
            raise ValueError("Required subscription fields cannot be cleared")
        return self


class VkSubscription(StoredRecord):
    owner_id: BigInt
    chat_id: BigInt
    last_post_id: BigInt
    with_reposts: bool
    with_header: bool
    is_suspended: bool
    description: str | None = None
