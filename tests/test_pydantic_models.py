import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from pydantic import ValidationError

from common.db.edb import ChatDB, EdgeDB, UpdateDB, UserDB
from hub_bot.db import EcosystemChat, VkWallPosting
from msu_hub_bot.settings import Settings

IDENTIFIER = UUID(int=1)


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
    monkeypatch.delenv("HUB_REDIS_PASSWORD", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("HUB_REDIS_PASSWORD=synthetic-dotenv-marker\n")
    assert Settings().redis_password == ""


def test_settings_validation_does_not_echo_invalid_input():
    marker = "synthetic-invalid-credential-marker"
    with pytest.raises(ValidationError) as result:
        Settings(redis_port=marker)
    assert marker not in str(result.value)
    assert marker not in repr(result.value)
    assert "redis_port" in str(result.value)


@pytest.mark.parametrize("count", [0, 4, 6])
def test_settings_preserve_five_entry_access_list(count):
    with pytest.raises(ValidationError):
        Settings(dvach_chat_ids=[0] * count)


def test_database_models_preserve_omitted_optional_fields_and_schema_names():
    user = UserDB(id=IDENTIFIER, user_id=101, is_bot=False, first_name="Тест")
    directory = EcosystemChat(id=IDENTIFIER, chat_id=-101, name="Synthetic", section="group", is_hidden=False)
    subscription = VkWallPosting(
        id=IDENTIFIER, owner_id=-202, chat_id=-101, last_post_id=303, with_reposts=True, with_header=False, is_suspended=False
    )
    assert (user.last_name, user.username, user.language_code) == (None, None, None)
    assert (directory.username_alias, directory.members, directory.pinned_message_id) == (None, None, None)
    assert subscription.description is None
    assert UserDB.type_name() == "telegram::User"
    assert ChatDB.type_name() == "telegram::Chat"
    assert UpdateDB.type_name() == "telegram::BotUpdate"
    assert EcosystemChat.type_name() == "msu_hub::EcosystemChat"
    assert VkWallPosting.type_name() == "vk_tg::VkWallPosting"
    assert UserDB.fields() == "id, user_id, is_bot, first_name, last_name, username, language_code"
    assert ChatDB.fields() == "id, chat_id, type, title, username, first_name, last_name, metadata"


@pytest.mark.parametrize("value", [{"settings": {"feature": True}, "unknown": None}, {}, [], None, "text", 7])
def test_database_json_keeps_legacy_objects_scalars_and_null(value):
    chat = ChatDB(id=IDENTIFIER, chat_id=-101, type="group", metadata=json.dumps(value))
    update = UpdateDB(id=IDENTIFIER, handled=True, data=json.dumps(value))
    assert chat.metadata == value
    assert update.data == value
    assert chat.model_dump()["metadata"] == value
    assert chat.title is None


def test_database_json_object_does_not_discard_unknown_fields():
    metadata = {"unknown": {"preserved": [None, "Тест"]}, "settings": {}}
    chat = ChatDB(id=IDENTIFIER, chat_id=-101, type="group", metadata=metadata)
    assert chat.metadata == metadata
    with pytest.raises(ValidationError):
        ChatDB(id=IDENTIFIER, chat_id=-101, type="group")
    with pytest.raises(ValidationError):
        ChatDB(id=IDENTIFIER, chat_id=-101, type="group", metadata="{invalid json")


@pytest.mark.asyncio
async def test_query_builder_parses_records_without_changing_query_shape():
    record = SimpleNamespace(id=IDENTIFIER, user_id=101, is_bot=False, first_name="Synthetic")
    database = EdgeDB.__new__(EdgeDB)
    database.client = SimpleNamespace(query=AsyncMock(return_value=[record]), query_single=AsyncMock(return_value=record))
    query = UserDB.query(database)
    rows = await query.get_all()
    single = await query.get(101)
    assert rows == [single]
    assert single.last_name is None
    database.client.query.assert_awaited_once_with(
        "select telegram::User {id, user_id, is_bot, first_name, last_name, username, language_code};"
    )
    database.client.query_single.assert_awaited_once_with(
        "select telegram::User {id, user_id, is_bot, first_name, last_name, username, language_code} "
        "filter {.user_id = <int64>$user_id} limit 1;",
        user_id=101,
    )
