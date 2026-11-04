"""Extract bounded entity observations without retaining message bodies in profiles."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from aiogram.client.default import Default
from aiogram.types import (
    CallbackQuery,
    Chat,
    ChatFullInfo,
    ChatJoinRequest,
    ChatMemberUpdated,
    Message,
    MessageOriginChannel,
    MessageOriginChat,
    MessageOriginUser,
    MessageReactionCountUpdated,
    MessageReactionUpdated,
    ReactionTypeCustomEmoji,
    ReactionTypeEmoji,
    ReactionTypePaid,
    TelegramObject,
    Update,
    User,
)
from pydantic import JsonValue, TypeAdapter

from msu_hub_bot.storage.models import (
    ArchivedUpdate,
    ChatObservation,
    MembershipBatch,
    MembershipObservation,
    MessageObservation,
    ReactionObservation,
    ReactionValue,
    TopicObservation,
    UserObservation,
)

_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])
_CHAT_FIELDS = ("type", "title", "username", "first_name", "last_name")
_USER_FIELDS = ("is_bot", "first_name", "last_name", "username", "language_code")
# Telegram omits these from a Chat/User only when they are unset, so absence clears a
# stale value. language_code is context-dependent and stays a sparse sighting.
_CLEARED_CHAT_FIELDS = ("title", "username", "first_name", "last_name")
_CLEARED_USER_FIELDS = ("last_name", "username")
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
    value: Any,
    cutoff: datetime | None,
    *,
    profile: bool = False,
    message_body: bool = False,
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
        if is_message and (not message_body or expired):
            # Every body has one independently expiring normalized owner. Raw
            # receipts and nested messages keep references even before expiry.
            references = {key: _wire(value[key], cutoff) for key in _MESSAGE_REFERENCES if key in value}
            for key in ("chat", "from", "sender_chat"):
                entity = value.get(key)
                if isinstance(entity, dict):
                    references[key] = {name: entity[name] for name in ("id", "type") if name in entity}
            return references
        return {
            key: _wire(item, cutoff, profile=profile)
            # Incoming LinkPreviewOptions can contain unresolved client defaults
            # for absent fields. They are configuration, not received JSON.
            for key, item in value.items()
            if not isinstance(item, Default) and not (profile and key == "pinned_message")
        }
    if isinstance(value, (tuple, list)):
        return [_wire(item, cutoff, profile=profile) for item in value]
    # Pydantic checks extras as well as the known Telegram fields before storage.
    return cast(JsonValue, value)


def _payload(
    value: TelegramObject,
    cutoff: datetime | None = None,
    *,
    profile: bool = False,
    message_body: bool = False,
) -> dict[str, JsonValue]:
    return _JSON_OBJECT.validate_python(
        _wire(
            value.model_dump(mode="python", by_alias=True, exclude_none=True),
            cutoff,
            profile=profile,
            message_body=message_body,
        )
    )


def reference_payload(value: dict[str, Any], *, message_body: bool = False) -> dict[str, Any]:
    """Replace message copies without coercing opaque JSON numbers during import.

    ``message_body`` retains only the root Message body; nested Message objects
    remain references. The caller owns original-date eligibility and serialization.
    """
    return cast(dict[str, Any], _wire(value, None, message_body=message_body))


def chat_observation(chat: Chat | ChatFullInfo, observed_at: datetime | None = None) -> ChatObservation:
    profile = _payload(chat, profile=True)
    return ChatObservation.model_validate(
        {
            "chat_id": chat.id,
            "observed_at": observed_at or datetime.now(UTC),
            "profile": profile,
            **dict.fromkeys(_CLEARED_CHAT_FIELDS),
            **{key: profile[key] for key in _CHAT_FIELDS if key in profile},
        }
    )


def _user_observation(user: User, observed_at: datetime) -> UserObservation:
    profile = _payload(user, profile=True)
    return UserObservation.model_validate(
        {
            "user_id": user.id,
            "observed_at": observed_at,
            "profile": profile,
            **dict.fromkeys(_CLEARED_USER_FIELDS),
            **{key: profile[key] for key in _USER_FIELDS if key in profile},
        }
    )


def _reaction_key(reaction: ReactionTypeEmoji | ReactionTypeCustomEmoji | ReactionTypePaid) -> str:
    match reaction:
        case ReactionTypeEmoji():
            return f"e:{reaction.emoji}"
        case ReactionTypeCustomEmoji():
            return f"c:{reaction.custom_emoji_id}"
        case ReactionTypePaid():
            return "paid"
    raise ValueError("Unsupported reaction type")


def _reaction_observation(update: Update) -> ReactionObservation | None:
    if actor := update.message_reaction:
        return ReactionObservation(
            kind="actor",
            chat_id=actor.chat.id,
            message_id=actor.message_id,
            event_at=actor.date,
            user_id=actor.user.id if actor.user is not None else None,
            actor_chat_id=actor.actor_chat.id if actor.actor_chat is not None else None,
            previous_active=any(not isinstance(reaction, ReactionTypePaid) for reaction in actor.old_reaction),
            reactions=[ReactionValue(key=_reaction_key(reaction)) for reaction in actor.new_reaction],
        )
    if counts := update.message_reaction_count:
        return ReactionObservation(
            kind="counts",
            chat_id=counts.chat.id,
            message_id=counts.message_id,
            event_at=counts.date,
            reactions=[ReactionValue(key=_reaction_key(reaction.type), count=reaction.total_count) for reaction in counts.reactions],
        )
    return None


def archive_observation(
    update: Update,
    handled: bool,
    *,
    received_at: datetime | None = None,
    retention_at: datetime | None = None,
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
        chat_id: int,
        user_id: int,
        observed_at: datetime,
        status: str | None = None,
        permissions: dict[str, JsonValue] | None = None,
        *,
        observation_source: str = "membership",
        status_observed_at: datetime | None = None,
        status_source: str | None = None,
        status_event_id: int | None = None,
        admin_lost_at: datetime | None = None,
    ) -> None:
        key = (chat_id, user_id)
        previous = memberships.get(key)
        values: dict[str, Any] = (
            previous.model_dump(exclude_unset=True)
            if previous is not None
            else {
                "chat_id": chat_id,
                "user_id": user_id,
            }
        )
        if (
            previous is None
            or observed_at > previous.observed_at
            or (observed_at == previous.observed_at and previous.observation_source == "reply" and observation_source != "reply")
        ):
            values.update(observed_at=observed_at, observation_source=observation_source)
        if admin_lost_at is not None and (previous is None or previous.admin_lost_at is None or admin_lost_at > previous.admin_lost_at):
            values["admin_lost_at"] = admin_lost_at
        if status is not None:
            stamp = status_observed_at or observed_at

            # Activity and status have independent clocks. A fresh reply must not
            # erase an older join notice discovered inside the same update.
            def order(timestamp: datetime, source: str | None, event_id: int | None) -> tuple[datetime, int, int]:
                return timestamp, 2 if source in {"chat_member", "my_chat_member"} else 1, event_id or 0

            if (
                previous is None
                or previous.status_observed_at is None
                or order(stamp, status_source, status_event_id)
                >= order(previous.status_observed_at, previous.status_source, previous.status_event_id)
            ):
                values.update(
                    status=status,
                    permissions=permissions or {},
                    status_observed_at=stamp,
                    status_source=status_source,
                    status_event_id=status_event_id,
                )
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
        elif isinstance(
            value,
            (
                ChatMemberUpdated,
                ChatJoinRequest,
                MessageOriginUser,
                MessageOriginChat,
                MessageOriginChannel,
                MessageReactionUpdated,
                MessageReactionCountUpdated,
            ),
        ):
            observed_at = min(observed_at, value.date)
        if isinstance(value, User):
            if value.id not in users or observed_at > users[value.id].observed_at:
                users[value.id] = _user_observation(value, observed_at)
        elif isinstance(value, (Chat, ChatFullInfo)):
            if value.id not in chats or observed_at > chats[value.id].observed_at:
                chats[value.id] = chat_observation(value, observed_at)
        elif isinstance(value, ChatMemberUpdated):
            permissions = {
                key: item
                for key, item in _payload(value.new_chat_member).items()
                if key != "user" and key in type(value.new_chat_member).model_fields
            }
            membership(
                value.chat.id,
                value.new_chat_member.user.id,
                observed_at,
                value.new_chat_member.status,
                permissions,
                status_source="my_chat_member" if value is update.my_chat_member else "chat_member",
                status_event_id=update.update_id,
                admin_lost_at=observed_at
                if value is update.my_chat_member
                and value.old_chat_member.status in {"creator", "administrator"}
                and value.new_chat_member.status not in {"creator", "administrator"}
                else None,
            )
        elif isinstance(value, CallbackQuery):
            if value.message is not None:
                membership(value.message.chat.id, value.from_user.id, observed_at, observation_source="callback")
        elif isinstance(value, MessageReactionUpdated):
            if value.user is not None:
                membership(value.chat.id, value.user.id, observed_at, observation_source="reaction")
        elif isinstance(value, ChatJoinRequest):
            membership(value.chat.id, value.from_user.id, observed_at, observation_source="join_request")
        elif isinstance(value, Message):
            if value.from_user is not None and value.sender_chat is None:
                membership(
                    value.chat.id,
                    value.from_user.id,
                    observed_at,
                    observation_source="message" if depth == 1 else "reply",
                )
            for user in value.new_chat_members or []:
                membership(
                    value.chat.id,
                    user.id,
                    observed_at,
                    "member",
                    status_observed_at=min(received_at, value.date),
                    status_source="service_join",
                    status_event_id=value.message_id,
                )
            if value.left_chat_member is not None:
                membership(
                    value.chat.id,
                    value.left_chat_member.id,
                    observed_at,
                    "left",
                    status_observed_at=min(received_at, value.date),
                    status_source="service_leave",
                    status_event_id=value.message_id,
                )
            if value.message_thread_id is not None:
                topic_values: dict[str, Any] = {
                    "chat_id": value.chat.id,
                    "thread_id": value.message_thread_id,
                    "observed_at": observed_at,
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
                    topics[key] = TopicObservation.model_validate(
                        {
                            **(previous.model_dump(exclude_unset=True) if previous is not None else {}),
                            **topic_values,
                        }
                    )
            if value.date > cutoff and len(messages) < 64:
                business_id = value.business_connection_id or ""
                key_message = (business_id, value.chat.id, value.message_id)
                messages.setdefault(
                    key_message,
                    MessageObservation(
                        chat_id=value.chat.id,
                        message_id=value.message_id,
                        sent_at=value.date,
                        edited_at=datetime.fromtimestamp(value.edit_date, UTC) if value.edit_date is not None else None,
                        observed_at=received_at,
                        business_connection_id=business_id,
                        sender_user_id=value.from_user.id if value.from_user is not None else None,
                        sender_chat_id=value.sender_chat.id if value.sender_chat is not None else None,
                        thread_id=value.message_thread_id,
                        reply_to_message_id=value.reply_to_message.message_id if value.reply_to_message else None,
                        data=_payload(value, cutoff, message_body=True),
                    ),
                )
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
        update_id=update.update_id,
        received_at=received_at,
        kind=kind,
        handled=handled,
        data=_payload(update, cutoff),
        users=list(users.values()),
        chats=list(chats.values()),
        memberships=list(memberships.values()),
        topics=list(topics.values()),
        messages=list(messages.values()),
        reaction=_reaction_observation(update),
    )


def membership_batch(update: Update, *, received_at: datetime | None = None) -> MembershipBatch | None:
    """Extract only direct status evidence for the durable polling inbox.

    Message bodies, nested replies, arbitrary profiles and receipt payloads
    stay out of the inbox. Ordinary archival retains their separate lifecycle.
    """
    event = update.chat_member or update.my_chat_member
    message = update.message or update.edited_message or update.channel_post or update.edited_channel_post
    received_at = received_at or datetime.now(UTC)
    memberships: dict[int, MembershipObservation] = {}
    if event is not None:
        new = event.new_chat_member
        subjects = {new.user.id: new.user}
        source_chat = event.chat
        stamp = min(received_at, event.date)
        permissions = _JSON_OBJECT.validate_python(
            _wire(new.model_dump(mode="python", include=set(type(new).model_fields) - {"user"}), None)
        )
        memberships[new.user.id] = MembershipObservation(
            chat_id=source_chat.id,
            user_id=new.user.id,
            observed_at=stamp,
            observation_source="membership",
            status=new.status,
            permissions=permissions,
            status_observed_at=stamp,
            status_source="my_chat_member" if update.my_chat_member is not None else "chat_member",
            status_event_id=update.update_id,
            admin_lost_at=stamp
            if update.my_chat_member is not None
            and event.old_chat_member.status in {"creator", "administrator"}
            and new.status not in {"creator", "administrator"}
            else None,
        )
    elif message is not None:
        source_chat = message.chat
        stamp = min(received_at, message.date)
        subjects = {user.id: user for user in message.new_chat_members or []}
        for user in subjects.values():
            memberships[user.id] = MembershipObservation(
                chat_id=source_chat.id,
                user_id=user.id,
                observed_at=stamp,
                observation_source="membership",
                status="member",
                permissions={},
                status_observed_at=stamp,
                status_source="service_join",
                status_event_id=message.message_id,
            )
        if message.left_chat_member is not None:
            user = message.left_chat_member
            subjects[user.id] = user
            memberships[user.id] = MembershipObservation(
                chat_id=source_chat.id,
                user_id=user.id,
                observed_at=stamp,
                observation_source="membership",
                status="left",
                permissions={},
                status_observed_at=stamp,
                status_source="service_leave",
                status_event_id=message.message_id,
            )
    else:
        return None
    if not subjects:
        return None
    chat = Chat.model_validate(source_chat.model_dump(include={"id", *_CHAT_FIELDS}))
    users = [
        _user_observation(User.model_validate(user.model_dump(include={"id", *_USER_FIELDS})), stamp).model_copy(update={"profile": {}})
        for user in subjects.values()
    ]
    return MembershipBatch(
        update_id=update.update_id,
        received_at=received_at,
        users=users,
        chats=[chat_observation(chat, stamp).model_copy(update={"profile": {}})],
        memberships=list(memberships.values()),
    )
