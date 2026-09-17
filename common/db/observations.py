"""Extract bounded entity observations without retaining message bodies in profiles."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from aiogram.client.default import Default
from aiogram.types import (
    Chat, ChatFullInfo, ChatMemberUpdated, Message, MessageOriginChannel,
    MessageOriginChat, MessageOriginUser, MessageReactionCountUpdated,
    MessageReactionUpdated, TelegramObject, Update, User,
)
from pydantic import JsonValue, TypeAdapter

from common.db.models import (
    ArchivedUpdate, ChatObservation, MembershipObservation, MessageObservation,
    TopicObservation, UserObservation,
)

_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])
_CHAT_FIELDS = ("type", "title", "username", "first_name", "last_name")
_USER_FIELDS = ("is_bot", "first_name", "last_name", "username", "language_code")
_MESSAGE_REFERENCES = ("message_id", "date", "edit_date", "message_thread_id", "business_connection_id")


def is_message_payload(value: dict[str, Any]) -> bool:
    """Distinguish bodies from Telegram origins and reaction events.

    These events also carry a chat, message ID and date. Their timestamps
    describe provenance or a reaction, not an independently stored body.
    """
    keys = value.keys()
    if not {"message_id", "date", "chat"} <= keys:
        return False
    # Unknown extra fields may contain a body; keep migration's coverage check
    # strict instead of assuming that a future shape is a bodyless event.
    if value.get("type") == "channel" and keys <= MessageOriginChannel.model_fields.keys():
        return False
    if {"old_reaction", "new_reaction"} <= keys and keys <= MessageReactionUpdated.model_fields.keys():
        return False
    return not ("reactions" in keys and keys <= MessageReactionCountUpdated.model_fields.keys())


def _wire(
    value: Any, cutoff: datetime | None, *, profile: bool = False, message_body: bool = False, legacy: bool = False,
) -> JsonValue:
    if isinstance(value, datetime):
        return int(value.timestamp())
    if isinstance(value, dict):
        is_message = is_message_payload(value)
        if is_message and profile:
            return None
        date = value.get("date")
        timestamp = date.timestamp() if isinstance(date, datetime) else date
        expired = cutoff is not None and isinstance(timestamp, (int, float)) and timestamp <= cutoff.timestamp()
        if is_message and not legacy and (not message_body or expired):
            # Every body has one independently expiring normalized owner. Raw
            # receipts and nested messages keep references even before expiry.
            references = {key: _wire(value[key], cutoff) for key in _MESSAGE_REFERENCES if key in value}
            for key in ("chat", "from", "sender_chat"):
                entity = value.get(key)
                if isinstance(entity, dict):
                    references[key] = {name: entity[name] for name in ("id", "type") if name in entity}
            return references
        return {
            key: _wire(item, cutoff, profile=profile, legacy=legacy)
            # Incoming LinkPreviewOptions can contain unresolved client defaults
            # for absent fields. They are configuration, not received JSON.
            for key, item in value.items() if not isinstance(item, Default) and not (profile and key == "pinned_message")
        }
    if isinstance(value, (tuple, list)):
        return [_wire(item, cutoff, profile=profile, legacy=legacy) for item in value]
    # Pydantic checks extras as well as the known Telegram fields before storage.
    return cast(JsonValue, value)


def _payload(
    value: TelegramObject, cutoff: datetime | None = None, *, profile: bool = False, message_body: bool = False, legacy: bool = False,
) -> dict[str, JsonValue]:
    return _JSON_OBJECT.validate_python(_wire(
        value.model_dump(mode="python", by_alias=True, exclude_none=True), cutoff,
        profile=profile, message_body=message_body, legacy=legacy,
    ))


def reference_payload(value: dict[str, Any], *, message_body: bool = False) -> dict[str, Any]:
    """Replace message copies without coercing opaque JSON numbers during import.

    ``message_body`` retains only the root Message body; nested Message objects
    remain references. The caller owns original-date eligibility and serialization.
    """
    return cast(dict[str, Any], _wire(value, None, message_body=message_body))


def chat_observation(chat: Chat | ChatFullInfo, observed_at: datetime | None = None) -> ChatObservation:
    profile = _payload(chat, profile=True)
    return ChatObservation.model_validate({
        "chat_id": chat.id, "observed_at": observed_at or datetime.now(UTC), "profile": profile,
        **{key: profile[key] for key in _CHAT_FIELDS if key in profile},
    })


def _user_observation(user: User, observed_at: datetime) -> UserObservation:
    profile = _payload(user, profile=True)
    return UserObservation.model_validate({
        "user_id": user.id, "observed_at": observed_at, "profile": profile,
        **{key: profile[key] for key in _USER_FIELDS if key in profile},
    })


def archive_observation(
    update: Update, handled: bool, *, received_at: datetime | None = None, retention_at: datetime | None = None,
) -> ArchivedUpdate:
    received_at = received_at or datetime.now(UTC)
    cutoff = (retention_at or received_at) - timedelta(days=30)
    users: dict[int, UserObservation] = {}
    chats: dict[int, ChatObservation] = {}
    memberships: dict[tuple[int, int], MembershipObservation] = {}
    topics: dict[tuple[int, int], TopicObservation] = {}
    messages: dict[tuple[str, int, int], MessageObservation] = {}
    queue: deque[tuple[object, int, datetime]] = deque([(update, 0, received_at)])
    visited: set[int] = set()

    def membership(
        chat_id: int, user_id: int, observed_at: datetime, status: str | None = None, permissions: dict[str, JsonValue] | None = None,
    ) -> None:
        key = (chat_id, user_id)
        previous = memberships.get(key)
        if previous is None or observed_at > previous.observed_at or (observed_at == previous.observed_at and status is not None):
            values: dict[str, Any] = {"chat_id": chat_id, "user_id": user_id, "observed_at": observed_at}
            if status is not None:
                values["status"] = status
            if permissions is not None:
                values["permissions"] = permissions
            memberships[key] = MembershipObservation.model_validate(values)

    while queue and len(visited) < 512:
        value, depth, observed_at = queue.popleft()
        if id(value) in visited or depth > 12:
            continue
        if isinstance(value, (TelegramObject, list, tuple)):
            visited.add(id(value))
        # An old embedded message is a historical profile snapshot. Its entities
        # must not overwrite fresher profiles simply because a callback arrived.
        if isinstance(value, Message):
            version = datetime.fromtimestamp(value.edit_date, UTC) if value.edit_date is not None else value.date
            observed_at = min(observed_at, version)
        elif isinstance(value, (ChatMemberUpdated, MessageOriginUser, MessageOriginChat, MessageOriginChannel)):
            observed_at = min(observed_at, value.date)
        if isinstance(value, User):
            if value.id not in users or observed_at > users[value.id].observed_at:
                users[value.id] = _user_observation(value, observed_at)
        elif isinstance(value, (Chat, ChatFullInfo)):
            if value.id not in chats or observed_at > chats[value.id].observed_at:
                chats[value.id] = chat_observation(value, observed_at)
        elif isinstance(value, ChatMemberUpdated):
            permissions = _payload(value.new_chat_member)
            permissions.pop("user", None)
            membership(value.chat.id, value.new_chat_member.user.id, observed_at, value.new_chat_member.status, permissions)
        elif isinstance(value, Message):
            if value.from_user is not None:
                membership(value.chat.id, value.from_user.id, observed_at)
            for user in value.new_chat_members or []:
                membership(value.chat.id, user.id, observed_at, "member")
            if value.left_chat_member is not None:
                membership(value.chat.id, value.left_chat_member.id, observed_at, "left")
            if value.message_thread_id is not None:
                topic_values: dict[str, Any] = {
                    "chat_id": value.chat.id, "thread_id": value.message_thread_id, "observed_at": observed_at,
                }
                topic = value.forum_topic_created or value.forum_topic_edited
                if topic is not None:
                    topic_values["profile"] = _payload(topic, profile=True)
                    if topic.name is not None:
                        topic_values["title"] = topic.name
                if value.forum_topic_closed:
                    topic_values["is_closed"] = True
                elif value.forum_topic_reopened or value.forum_topic_created:
                    topic_values["is_closed"] = False
                key = (value.chat.id, value.message_thread_id)
                previous = topics.get(key)
                if previous is None or observed_at >= previous.observed_at:
                    topics[key] = TopicObservation.model_validate({
                        **(previous.model_dump(exclude_unset=True) if previous is not None else {}), **topic_values,
                    })
            if value.date > cutoff and len(messages) < 64:
                business_id = value.business_connection_id or ""
                key_message = (business_id, value.chat.id, value.message_id)
                messages.setdefault(key_message, MessageObservation(
                    chat_id=value.chat.id, message_id=value.message_id, sent_at=value.date,
                    edited_at=datetime.fromtimestamp(value.edit_date, UTC) if value.edit_date is not None else None,
                    observed_at=received_at, business_connection_id=business_id,
                    sender_user_id=value.from_user.id if value.from_user is not None else None,
                    sender_chat_id=value.sender_chat.id if value.sender_chat is not None else None,
                    thread_id=value.message_thread_id,
                    reply_to_message_id=value.reply_to_message.message_id if value.reply_to_message else None,
                    data=_payload(value, cutoff, message_body=True),
                ))
        children: Iterable[object] = ()
        if isinstance(value, TelegramObject):
            children = (getattr(value, name) for name in type(value).model_fields)
        elif isinstance(value, (list, tuple)):
            children = value
        for child in children:
            if len(queue) + len(visited) >= 512:
                break
            if isinstance(child, (TelegramObject, list, tuple)):
                queue.append((child, depth + 1, observed_at))

    # A traversal limit must never emit dangling relationships.
    memberships = {key: item for key, item in memberships.items() if item.chat_id in chats and item.user_id in users}
    topics = {key: item for key, item in topics.items() if item.chat_id in chats}
    kind = next((name for name in type(update).model_fields if name != "update_id" and getattr(update, name) is not None), "unknown")
    return ArchivedUpdate(
        update_id=update.update_id, received_at=received_at, kind=kind, handled=handled, data=_payload(update, cutoff),
        legacy_data=_payload(update, legacy=True),
        users=list(users.values()), chats=list(chats.values()), memberships=list(memberships.values()),
        topics=list(topics.values()), messages=list(messages.values()),
    )
