import json
from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from msu_hub_bot.storage.models import ChatRecord, DirectoryRecord, UserRecord, VkSubscription
from msu_hub_bot.settings import Settings

IDENTIFIER = UUID(int=1)
CREATED = datetime(2026, 9, 17, tzinfo=UTC)


def test_settings_preserve_json_collections_case_and_init_precedence(monkeypatch):
    monkeypatch.delenv("HUB_OWNER_ID", raising=False)
    monkeypatch.setenv("hub_owner_id", "11")
    monkeypatch.setenv("HUB_FORWARD_CHAT_IDS", "[21, 22]")
    monkeypatch.setenv("HUB_JDOODLE_TOKENS", '[["synthetic-client", "synthetic-secret"]]')
    config = Settings()
    assert config.owner_id == 11
    assert config.forward_chat_ids == (21, 22)
    assert config.jdoodle_tokens == [("synthetic-client", "synthetic-secret")]
    assert Settings(owner_id=12).owner_id == 12


def test_settings_do_not_read_ambient_dotenv_files(monkeypatch, tmp_path):
    monkeypatch.delenv("HUB_SUPABASE_PASSWORD", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("HUB_SUPABASE_PASSWORD=synthetic-dotenv-marker\n")
    assert Settings().supabase_password == ""


def test_settings_validation_does_not_echo_invalid_input():
    marker = "synthetic-invalid-credential-marker"
    with pytest.raises(ValidationError) as result:
        Settings(web_port=marker)
    assert marker not in str(result.value)
    assert marker not in repr(result.value)
    assert "web_port" in str(result.value)


@pytest.mark.parametrize("count", [0, 4, 6])
def test_settings_preserve_five_entry_access_list(count):
    with pytest.raises(ValidationError):
        Settings(dvach_chat_ids=[0] * count)


def test_database_models_preserve_omitted_optional_fields():
    user = UserRecord(id=IDENTIFIER, created=CREATED, user_id=101, is_bot=False, first_name="Тест", metadata={})
    directory = DirectoryRecord(id=IDENTIFIER, created=CREATED, chat_id=-101, name="Synthetic", section="group", is_hidden=False)
    subscription = VkSubscription(
        id=IDENTIFIER,
        created=CREATED,
        owner_id=-202,
        chat_id=-101,
        last_post_id=303,
        with_reposts=True,
        with_header=False,
        is_suspended=False,
    )
    assert (user.last_name, user.username, user.language_code) == (None, None, None)
    assert (directory.username_alias, directory.members, directory.pinned_message_id) == (None, None, None)
    assert subscription.description is None


@pytest.mark.parametrize("value", [{"settings": {"feature": True}, "unknown": None}, {}, [], None, "text", 7])
def test_database_json_keeps_legacy_objects_scalars_and_null(value):
    chat = ChatRecord.model_validate_json(
        json.dumps(
            {
                "id": str(IDENTIFIER),
                "created": CREATED.isoformat(),
                "chat_id": -101,
                "type": "group",
                "metadata": value,
            }
        )
    )
    assert chat.metadata == value
    assert chat.model_dump()["metadata"] == value
    assert chat.title is None


def test_database_json_object_does_not_discard_unknown_fields():
    metadata = {"unknown": {"preserved": [None, "Тест"]}, "settings": {}}
    chat = ChatRecord(id=IDENTIFIER, created=CREATED, chat_id=-101, type="group", metadata=metadata)
    assert chat.metadata == metadata
    with pytest.raises(ValidationError):
        ChatRecord(id=IDENTIFIER, created=CREATED, chat_id=-101, type="group")
    with pytest.raises(ValidationError):
        ChatRecord.model_validate_json("{invalid json")
