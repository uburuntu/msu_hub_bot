from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from aiogram.types import CallbackQuery, Chat, ChatFullInfo, Message, Update, User

from common.db.observations import archive_observation, chat_observation, is_message_payload, reference_payload

NOW = datetime(2026, 9, 17, tzinfo=UTC)


def user(number=10):
    return User(id=number, is_bot=False, first_name=f"User {number}")


def message(**values):
    if "from_user" in values:
        values["from"] = values.pop("from_user")
    return Message.model_validate(
        {
            "message_id": 1,
            "date": NOW - timedelta(minutes=2),
            "chat": {"id": -1001, "type": "supergroup", "title": "Synthetic"},
            "from": user(),
            "text": "current-body",
            **values,
        }
    )


def test_archive_extracts_reply_forward_entity_and_membership_users():
    source = message(
        reply_to_message=message(message_id=2, from_user=user(20)),
        forward_origin={"type": "user", "date": NOW - timedelta(days=1), "sender_user": user(30)},
        entities=[{"type": "text_mention", "offset": 0, "length": 4, "user": user(40)}],
        new_chat_members=[user(50)],
        left_chat_member=user(60),
        sender_chat={"id": -1002, "type": "channel", "title": "Source"},
        message_thread_id=7,
        forum_topic_created={"name": "Synthetic topic", "icon_color": 1},
    )
    row = archive_observation(Update(update_id=1, message=source), True, received_at=NOW)
    assert {item.user_id for item in row.users} == {10, 20, 30, 40, 50, 60}
    assert {item.chat_id for item in row.chats} == {-1001, -1002}
    memberships = {item.user_id: item for item in row.memberships}
    assert memberships[50].status == "member" and memberships[60].status == "left"
    assert "status" not in memberships[10].model_fields_set
    assert row.topics[0].title == "Synthetic topic" and row.topics[0].is_closed is False
    assert {item.message_id for item in row.messages} == {1, 2}
    assert row.messages[0].reply_to_message_id == 2
    assert row.messages[0].sender_chat_id == -1002
    assert row.data["message"]["from"]["id"] == 10
    assert row.data["message"]["date"] == int(source.date.timestamp())
    assert "current-body" not in repr(row.data)
    assert "current-body" not in repr([item.profile for item in row.chats + row.users])


def test_old_callback_message_retains_entities_without_extending_body_retention():
    old = message(date=NOW - timedelta(days=31), text="EXPIRED_BODY_CANARY", caption="EXPIRED_CAPTION_CANARY")
    callback = CallbackQuery(id="test", from_user=user(99), message=old, chat_instance="test", data="roll:0")
    row = archive_observation(Update(update_id=2, callback_query=callback), False, received_at=NOW)
    assert {item.user_id for item in row.users} == {10, 99}
    assert next(item for item in row.users if item.user_id == 99).observed_at == NOW
    assert next(item for item in row.users if item.user_id == 10).observed_at == old.date
    assert row.messages == []
    assert "EXPIRED_" not in repr(row.model_dump())
    retained = row.data["callback_query"]["message"]
    assert retained["message_id"] == 1 and retained["chat"]["id"] == -1001


def test_current_reply_and_normalized_message_remove_expired_nested_body():
    old = message(message_id=2, date=NOW - timedelta(days=30), text="EXPIRED_REPLY_CANARY")
    current = message(reply_to_message=old, edit_date=int((NOW - timedelta(seconds=10)).timestamp()))
    row = archive_observation(Update(update_id=3, edited_message=current), True, received_at=NOW)
    assert row.kind == "edited_message" and len(row.messages) == 1
    assert row.messages[0].edited_at == NOW - timedelta(seconds=10)
    assert "EXPIRED_REPLY_CANARY" not in repr(row.model_dump())
    assert row.messages[0].data["text"] == "current-body"


def test_near_expiry_embedded_body_has_one_owner_and_cannot_survive_in_new_receipts():
    old = message(message_id=2, date=NOW - timedelta(days=29), text="NEAR_EXPIRY_BODY_CANARY")
    current = message(reply_to_message=old)
    update = Update(update_id=5, message=current)
    before = archive_observation(update, True, received_at=NOW)
    assert "NEAR_EXPIRY_BODY_CANARY" not in repr(before.data)
    by_id = {item.message_id: item for item in before.messages}
    assert "NEAR_EXPIRY_BODY_CANARY" not in repr(by_id[1].data)
    assert by_id[1].data["reply_to_message"]["message_id"] == 2
    assert by_id[2].data["text"] == "NEAR_EXPIRY_BODY_CANARY"
    assert by_id[2].sent_at == NOW - timedelta(days=29)
    after = archive_observation(update, True, received_at=NOW + timedelta(days=1))
    assert {item.message_id for item in after.messages} == {1}
    assert "NEAR_EXPIRY_BODY_CANARY" not in repr(after.model_dump())


def test_near_expiry_callback_has_no_receipt_copy_of_message_content():
    old = message(date=NOW - timedelta(days=29), text="CALLBACK_BODY_CANARY")
    callback = CallbackQuery(id="test", from_user=user(99), message=old, chat_instance="test", data="non-message-event-data")
    row = archive_observation(Update(update_id=6, callback_query=callback), True, received_at=NOW)
    assert "CALLBACK_BODY_CANARY" not in repr(row.data)
    assert row.messages[0].data["text"] == "CALLBACK_BODY_CANARY"
    assert row.data["callback_query"]["data"] == "non-message-event-data"


def test_editing_an_expired_top_level_message_never_renews_body_retention():
    old = message(date=NOW - timedelta(days=31), edit_date=int(NOW.timestamp()), text="EXPIRED_EDIT_BODY_CANARY")
    row = archive_observation(Update(update_id=7, edited_message=old), True, received_at=NOW)
    assert row.messages == [] and row.kind == "edited_message"
    assert "EXPIRED_EDIT_BODY_CANARY" not in repr(row.model_dump())
    assert row.data["edited_message"]["edit_date"] == int(NOW.timestamp())
    assert row.legacy_data["edited_message"]["text"] == "EXPIRED_EDIT_BODY_CANARY"
    assert "EXPIRED_EDIT_BODY_CANARY" not in repr(row)
    assert "legacy_data" not in row.model_dump(mode="json")


def test_import_retention_time_is_independent_from_historical_receipt_time():
    old_time = NOW - timedelta(days=60)
    old = message(date=old_time, text="OLD_IMPORT_BODY_CANARY")
    row = archive_observation(Update(update_id=8, message=old), True, received_at=old_time, retention_at=NOW)
    assert row.received_at == old_time and row.users[0].observed_at == old_time
    assert row.messages == []
    assert "OLD_IMPORT_BODY_CANARY" not in repr(row.model_dump())


def test_import_reference_transform_preserves_numeric_lexical_values_without_mutating_input():
    precise = Decimal("12345678901234567890.12345678901234567890")
    nested = {"message_id": 2, "date": 1, "chat": {"id": -1001, "type": "supergroup"}, "text": "nested-body"}
    body = {
        "message_id": 1,
        "date": 2,
        "chat": {"id": -1001, "type": "supergroup"},
        "text": "root-body",
        "opaque_numeric": precise,
        "reply_to_message": nested,
    }
    envelope = {"update_id": 9, "message": body, "opaque_numeric": precise}
    receipt = reference_payload(envelope)
    assert receipt["opaque_numeric"] is precise
    assert "root-body" not in repr(receipt) and "nested-body" not in repr(receipt)
    normalized = reference_payload(body, message_body=True)
    assert normalized["opaque_numeric"] is precise and normalized["text"] == "root-body"
    assert normalized["reply_to_message"]["message_id"] == 2
    assert "nested-body" not in repr(normalized)
    assert envelope["message"]["reply_to_message"]["text"] == "nested-body"


def test_chat_profile_omits_pinned_message_and_absent_optional_properties():
    chat = ChatFullInfo(
        id=-1001,
        type="supergroup",
        title="Synthetic",
        accent_color_id=1,
        max_reaction_count=1,
        accepted_gift_types={
            "unlimited_gifts": True,
            "limited_gifts": True,
            "unique_gifts": True,
            "premium_subscription": True,
            "gifts_from_channels": True,
        },
        pinned_message=message(text="PINNED_BODY_CANARY"),
    )
    observation = chat_observation(chat, NOW)
    assert "pinned_message" not in observation.profile
    assert "PINNED_BODY_CANARY" not in repr(observation.model_dump())
    assert "username" not in observation.model_fields_set
    assert "username" not in observation.model_dump(exclude_unset=True)
    assert observation.profile["accent_color_id"] == 1


def test_channel_origin_keeps_provenance_without_creating_a_message_body():
    origin = {
        "type": "channel", "date": int((NOW - timedelta(days=1)).timestamp()),
        "chat": {"id": -1002, "type": "channel", "title": "Synthetic source"},
        "message_id": 42, "author_signature": "Synthetic author",
    }
    source = message(forward_origin=origin)
    row = archive_observation(Update(update_id=10, message=source), True, received_at=NOW)
    assert {item.message_id for item in row.messages} == {1}
    assert row.messages[0].data["forward_origin"] == origin
    assert reference_payload({"forward_origin": origin})["forward_origin"] == origin
    assert row.legacy_data["message"]["forward_origin"] == origin
    assert next(item for item in row.chats if item.chat_id == -1002).observed_at == NOW - timedelta(days=1)


@pytest.mark.parametrize("kind,event", [
    ("message_reaction", {"old_reaction": [], "new_reaction": [{"type": "emoji", "emoji": "👍"}],
                          "user": {"id": 10, "is_bot": False, "first_name": "Synthetic"}}),
    ("message_reaction_count", {"reactions": [{"type": {"type": "emoji", "emoji": "👍"}, "total_count": 2}]}),
])
def test_reaction_receipts_preserve_event_fields_without_inventing_message_bodies(kind, event):
    data = {"chat": {"id": -1001, "type": "supergroup"}, "message_id": 42,
            "date": int(NOW.timestamp()), **event}
    row = archive_observation(Update.model_validate({"update_id": 11, kind: data}), False, received_at=NOW)
    assert row.kind == kind and row.messages == []
    assert row.data[kind] == data
    assert reference_payload({kind: data}) == {kind: data}


@pytest.mark.parametrize("extra", [{"type": "channel"}, {"old_reaction": [], "new_reaction": []}, {"reactions": []}])
def test_unknown_shapes_with_body_fields_still_require_message_coverage(extra):
    value = {"message_id": 42, "date": int(NOW.timestamp()), "chat": {"id": -1001, "type": "supergroup"},
             "text": "UNKNOWN_BODY_CANARY", **extra}
    assert is_message_payload(value)
    assert "UNKNOWN_BODY_CANARY" not in repr(reference_payload({"unknown": value}))


@pytest.mark.parametrize("options", [{}, {"is_disabled": True}, {"is_disabled": False, "show_above_text": False}])
def test_link_preview_preserves_received_values_and_omits_unresolved_client_defaults(options):
    source = message(link_preview_options=options)
    row = archive_observation(Update(update_id=12, message=source), True, received_at=NOW)
    assert row.messages[0].data["link_preview_options"] == options
    assert row.legacy_data["message"]["link_preview_options"] == options
    assert "Default(" not in row.model_dump_json()


def test_membership_update_observes_actor_and_subject_separately():
    row = archive_observation(
        Update.model_validate(
            {
                "update_id": 4,
                "chat_member": {
                    "chat": Chat(id=-1001, type="supergroup", title="Synthetic"),
                    "from": user(10),
                    "date": NOW,
                    "old_chat_member": {"status": "member", "user": user(20)},
                    "new_chat_member": {
                        "status": "restricted",
                        "user": user(20),
                        "is_member": True,
                        "can_send_messages": False,
                        "can_send_audios": False,
                        "can_send_documents": False,
                        "can_send_photos": False,
                        "can_send_videos": False,
                        "can_send_video_notes": False,
                        "can_send_voice_notes": False,
                        "can_send_polls": False,
                        "can_send_other_messages": False,
                        "can_add_web_page_previews": False,
                        "can_change_info": False,
                        "can_invite_users": False,
                        "can_react_to_messages": False,
                        "can_edit_tag": False,
                        "can_pin_messages": False,
                        "can_manage_topics": False,
                        "until_date": 0,
                    },
                },
            }
        ),
        True,
        received_at=NOW,
    )
    assert {item.user_id for item in row.users} == {10, 20}
    assert len(row.memberships) == 1
    membership = row.memberships[0]
    assert membership.user_id == 20 and membership.status == "restricted"
    assert membership.permissions["can_send_messages"] is False
    assert "user" not in membership.permissions
