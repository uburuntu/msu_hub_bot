from datetime import UTC, datetime, timedelta

from aiogram.types import CallbackQuery, Chat, ChatFullInfo, Message, Update, User

from common.db.observations import archive_observation, chat_observation

NOW = datetime(2026, 9, 17, tzinfo=UTC)


def user(number=10):
    return User(id=number, is_bot=False, first_name=f"User {number}")


def message(**values):
    if "from_user" in values:
        values["from"] = values.pop("from_user")
    return Message.model_validate({
        "message_id": 1, "date": NOW - timedelta(minutes=2),
        "chat": {"id": -1001, "type": "supergroup", "title": "Synthetic"},
        "from": user(), "text": "current-body", **values,
    })


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


def test_chat_profile_omits_pinned_message_and_absent_optional_properties():
    chat = ChatFullInfo(
        id=-1001, type="supergroup", title="Synthetic", accent_color_id=1, max_reaction_count=1,
        accepted_gift_types={"unlimited_gifts": True, "limited_gifts": True, "unique_gifts": True, "premium_subscription": True,
            "gifts_from_channels": True},
        pinned_message=message(text="PINNED_BODY_CANARY"),
    )
    observation = chat_observation(chat, NOW)
    assert "pinned_message" not in observation.profile
    assert "PINNED_BODY_CANARY" not in repr(observation.model_dump())
    assert "username" not in observation.model_fields_set
    assert "username" not in observation.model_dump(exclude_unset=True)
    assert observation.profile["accent_color_id"] == 1


def test_membership_update_observes_actor_and_subject_separately():
    row = archive_observation(Update.model_validate({
        "update_id": 4,
        "chat_member": {
            "chat": Chat(id=-1001, type="supergroup", title="Synthetic"), "from": user(10), "date": NOW,
            "old_chat_member": {"status": "member", "user": user(20)},
            "new_chat_member": {"status": "restricted", "user": user(20), "is_member": True,
                "can_send_messages": False, "can_send_audios": False, "can_send_documents": False,
                "can_send_photos": False, "can_send_videos": False, "can_send_video_notes": False,
                "can_send_voice_notes": False, "can_send_polls": False, "can_send_other_messages": False,
                "can_add_web_page_previews": False, "can_change_info": False, "can_invite_users": False,
                "can_react_to_messages": False, "can_edit_tag": False,
                "can_pin_messages": False, "can_manage_topics": False, "until_date": 0},
        },
    }), True, received_at=NOW)
    assert {item.user_id for item in row.users} == {10, 20}
    assert len(row.memberships) == 1
    membership = row.memberships[0]
    assert membership.user_id == 20 and membership.status == "restricted"
    assert membership.permissions["can_send_messages"] is False
    assert "user" not in membership.permissions
