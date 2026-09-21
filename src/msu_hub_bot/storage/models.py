"""Database-independent records and sparse observations of Telegram entities."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal, Self
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, StrictBool, field_validator, model_validator

BigInt = Annotated[int, Field(strict=True, ge=-(2**63), lt=2**63)]
PositiveBigInt = Annotated[int, Field(strict=True, gt=0, lt=2**63)]
MembershipState = Literal["present", "absent", "unknown"]
MembershipObservationSource = Literal["message", "reply", "callback", "reaction", "join_request", "membership"]
MembershipStatusSource = Literal["chat_member", "my_chat_member", "service_join", "service_leave"]


def utc_now() -> datetime:
    return datetime.now(UTC)


class DatabaseModel(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class StoredResponse(DatabaseModel):
    # SELECT responses may gain columns before an older application is replaced.
    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)


class StoredRecord(StoredResponse):
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
    observation_source: MembershipObservationSource | None = None
    status: str | None = None
    status_observed_at: AwareDatetime | None = None
    status_source: MembershipStatusSource | None = None
    status_event_id: BigInt | None = None
    admin_lost_at: AwareDatetime | None = None
    permissions: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def status_evidence(self) -> Self:
        has_clock = any(value is not None for value in (self.status_source, self.status_observed_at, self.status_event_id))
        if has_clock and (
            self.status_source is None
            or self.status not in {"creator", "administrator", "member", "restricted", "left", "kicked"}
            or self.status_observed_at is None
            or self.status_event_id is None
        ):
            raise ValueError("Explicit membership evidence requires a complete status clock")
        if self.status is None and (self.status_observed_at is not None or self.status_event_id is not None):
            raise ValueError("A membership status clock requires status evidence")
        if self.admin_lost_at is not None and self.status_source != "my_chat_member":
            raise ValueError("Admin loss requires the bot's membership evidence")
        return self


class MembershipBatch(DatabaseModel):
    """Durable membership evidence without raw updates, messages or receipts."""

    update_id: BigInt
    received_at: AwareDatetime = Field(default_factory=utc_now)
    users: list[UserObservation] = Field(default_factory=list, max_length=512)
    chats: list[ChatObservation] = Field(default_factory=list, max_length=512)
    memberships: list[MembershipObservation] = Field(default_factory=list, max_length=512)


class ChatMemberRecord(StoredResponse):
    chat_id: BigInt
    user_id: BigInt
    is_bot: StrictBool
    first_name: str
    last_name: str | None
    username: str | None
    state: MembershipState
    status: str | None
    status_observed_at: AwareDatetime | None
    status_source: MembershipStatusSource | Literal["legacy"] | None
    status_event_id: BigInt | None
    is_member: StrictBool | None
    observation_source: MembershipObservationSource | None
    first_seen_at: AwareDatetime
    last_seen_at: AwareDatetime


class MembershipCoverage(StoredResponse):
    """Observed bot availability cannot establish a complete Telegram roster."""

    complete: Literal[False]
    observed_count: Annotated[int, Field(strict=True, ge=0, lt=2**63)]
    present_count: Annotated[int, Field(strict=True, ge=0, lt=2**63)]
    absent_count: Annotated[int, Field(strict=True, ge=0, lt=2**63)]
    unknown_count: Annotated[int, Field(strict=True, ge=0, lt=2**63)]
    bot_state: MembershipState
    bot_status: str | None
    bot_status_observed_at: AwareDatetime | None
    bot_status_source: MembershipStatusSource | Literal["legacy"] | None
    bot_is_admin: StrictBool | None
    admin_lost_at: AwareDatetime | None


class ChatMemberPage(StoredResponse):
    chat_id: BigInt
    state: MembershipState | None
    members: list[ChatMemberRecord] = Field(max_length=100)
    next_after_user_id: BigInt | None
    coverage: MembershipCoverage


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


class FeedbackMessageRecord(DatabaseModel):
    """The archive's narrow feedback projection, never a raw message payload."""

    chat_id: BigInt
    message_id: PositiveBigInt
    sent_at: AwareDatetime
    thread_id: PositiveBigInt | None
    author_id: BigInt | None
    author_kind: Literal["user", "chat", "unknown"]
    author_name: str = Field(max_length=128)
    text: str = Field(max_length=800)
    media_kind: (
        Literal[
            "rich_message",
            "photo",
            "video",
            "document",
            "audio",
            "voice",
            "animation",
            "video_note",
            "sticker",
            "contact",
            "location",
            "venue",
            "poll",
            "dice",
        ]
        | None
    )
    truncated: StrictBool


class ReactionValue(DatabaseModel):
    key: Annotated[str, Field(strict=True, min_length=1, max_length=256)]
    count: PositiveBigInt = 1

    @field_validator("key")
    @classmethod
    def canonical_key(cls, value: str) -> str:
        if value != "paid" and not (value.startswith(("e:", "c:")) and len(value) > 2):
            raise ValueError("Reaction key requires an emoji, custom emoji or paid type")
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError("Reaction keys cannot contain control characters")
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("Reaction keys require valid Unicode") from None
        return value


class ReactionObservation(DatabaseModel):
    """One actor selection or an anonymous message count snapshot."""

    kind: Literal["actor", "counts"]
    chat_id: BigInt
    message_id: PositiveBigInt
    event_at: AwareDatetime
    user_id: BigInt | None = None
    actor_chat_id: BigInt | None = None
    previous_active: StrictBool | None = None
    reactions: list[ReactionValue] = Field(max_length=256)

    @model_validator(mode="after")
    def snapshot_contract(self) -> Self:
        actors = int(self.user_id is not None) + int(self.actor_chat_id is not None)
        if actors != (1 if self.kind == "actor" else 0):
            raise ValueError("Actor snapshots require exactly one actor; count snapshots have none")
        if (self.previous_active is not None) != (self.kind == "actor"):
            raise ValueError("Only actor snapshots require a previous selection status")
        unique: dict[str, ReactionValue] = {}
        for reaction in self.reactions:
            if self.kind == "actor" and reaction.count != 1:
                raise ValueError("An actor can select each reaction only once")
            previous = unique.setdefault(reaction.key, reaction)
            if previous.count != reaction.count:
                raise ValueError("A reaction snapshot cannot contain conflicting counts")
        self.reactions = list(unique.values())
        return self


class ArchivedUpdate(DatabaseModel):
    id: UUID = Field(default_factory=uuid4)
    update_id: BigInt
    received_at: AwareDatetime = Field(default_factory=utc_now)
    kind: str
    handled: bool
    data: dict[str, JsonValue]
    users: list[UserObservation] = Field(default_factory=list)
    chats: list[ChatObservation] = Field(default_factory=list)
    memberships: list[MembershipObservation] = Field(default_factory=list)
    topics: list[TopicObservation] = Field(default_factory=list)
    messages: list[MessageObservation] = Field(default_factory=list)
    reaction: ReactionObservation | None = None


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
