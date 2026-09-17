import asyncio
import json
from collections import deque
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from msu_hub_bot.storage.edgedb import EdgeDBRepository
from msu_hub_bot.storage.models import ArchivedUpdate, ChatObservation, DirectoryCreate, DirectoryPatch, VkPatch
from msu_hub_bot.settings import Settings

NOW = datetime(2026, 9, 17, tzinfo=UTC)


@pytest.mark.parametrize("tls_ca", ["", "synthetic-certificate"])
async def test_configured_client_preserves_dsn_tls_options_and_cleanup(monkeypatch, tls_ca):
    client = SimpleNamespace(aclose=AsyncMock())
    factory = Mock(return_value=client)
    monkeypatch.setattr("edgedb.create_async_client", factory)
    config = Settings(edgedb_dsn="edgedb://localhost/synthetic", edgedb_tls_ca=tls_ca, edgedb_tls_security="strict")
    repository = EdgeDBRepository(config=config)
    factory.assert_called_once_with(dsn=config.edgedb_dsn, tls_ca=tls_ca or None, tls_security="strict")
    assert repository.client is client
    await repository.close()
    client.aclose.assert_awaited_once()


def chat_row(metadata):
    return {
        "id": "00000000-0000-0000-0000-000000000001",
        "created": NOW.isoformat(),
        "chat_id": -1001,
        "type": "supergroup",
        "title": "Synthetic",
        "metadata": metadata,
    }


def directory_row(**values):
    return {
        "id": "00000000-0000-0000-0000-000000000002",
        "created": NOW.isoformat(),
        "chat_id": -1001,
        "name": "Synthetic",
        "section": "other",
        "is_hidden": False,
        **values,
    }


class JsonClient:
    def __init__(self, *responses):
        self.responses = deque(responses)
        self.calls = []
        self.in_transaction = False
        self.committed = False
        self.aclose = AsyncMock()

    async def query_single_json(self, query, **values):
        self.calls.append((query, values, self.in_transaction))
        result = self.responses.popleft()
        if isinstance(result, BaseException):
            raise result
        return json.dumps(result)

    query_json = query_single_json

    async def transaction(self):
        yield self

    async def __aenter__(self):
        self.in_transaction = True
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        self.committed = exc_type is None
        self.in_transaction = False


async def test_missing_records_are_none_and_empty_collection_is_list():
    client = JsonClient(None, None, [])
    repository = EdgeDBRepository(client=client)
    assert await repository.get_chat(11) is None
    assert await repository.get_directory(11) is None
    assert await repository.list_directory() == []
    assert all("created" in call[0] for call in client.calls)


async def test_settings_load_only_initializes_a_missing_chat_without_refreshing_old_callback_fields():
    row = chat_row({"settings": {"with_nsfw": True}})
    client = JsonClient(row, row)
    repository = EdgeDBRepository(client=client)
    observation = ChatObservation(chat_id=-1001, type="supergroup", title="Old callback snapshot")
    assert await repository.load_settings(observation) == {"with_nsfw": True}
    assert "else (select telegram::Chat)" in client.calls[0][0]
    await repository.ensure_chat(observation)
    assert "else (update telegram::Chat set" in client.calls[1][0]


async def test_vk_upsert_returns_complete_record_and_omitted_fields_keep_defaults():
    row = {
        "id": "00000000-0000-0000-0000-000000000003",
        "created": NOW.isoformat(),
        "owner_id": -10,
        "chat_id": -20,
        "last_post_id": 9,
        "with_reposts": False,
        "with_header": True,
        "is_suspended": False,
        "description": "Synthetic",
    }
    client = JsonClient(row)
    record = await EdgeDBRepository(client=client).upsert_vk_subscription(-10, -20, VkPatch(with_header=False))
    query, parameters, _ = client.calls[0]
    assert record.chat_id == -20 and record.last_post_id == 9
    assert parameters == {"owner_id": -10, "chat_id": -20, "with_header": False}
    assert "created" in query and "description" in query and "unless conflict on ((.owner_id, .chat_id))" in query


async def test_directory_patch_distinguishes_clear_from_omitted_and_returns_record():
    client = JsonClient(directory_row(username_alias=None))
    record = await EdgeDBRepository(client=client).patch_directory(-1001, DirectoryPatch(username_alias=None))
    query, parameters, _ = client.calls[0]
    assert record is not None and record.username_alias is None
    assert parameters == {"chat_id": -1001, "username_alias": None}
    assert "username_alias := <optional str>$username_alias" in query
    assert "members :=" not in query


async def test_directory_create_preserves_existing_entry_when_adds_race():
    client = JsonClient(directory_row(name="Existing directory name"))
    entry = await EdgeDBRepository(client=client).create_directory(DirectoryCreate(chat_id=-1001, name="Racing add"))
    assert entry.name == "Existing directory name"
    assert "unless conflict on .chat_id else (select msu_hub::EcosystemChat)" in client.calls[0][0]


@pytest.mark.parametrize("metadata", [None, "{}", [1, {"nested": True}], 7, False])
async def test_settings_preserve_every_nonobject_legacy_metadata_shape(metadata):
    client = JsonClient(chat_row(metadata), {"id": chat_row(metadata)["id"]})
    result = await EdgeDBRepository(client=client).patch_settings(-1001, {"with_nsfw": True})
    saved = json.loads(client.calls[-1][1]["metadata"])
    assert saved == {"_legacy_metadata": metadata, "settings": {"with_nsfw": True}}
    assert result == {"with_nsfw": True}
    assert all(in_transaction for _, _, in_transaction in client.calls)
    assert client.committed


class MetadataClient(JsonClient):
    def __init__(self):
        super().__init__()
        self.metadata = {"other": {"value": 7}, "settings": {"future_option": [1, 2]}}
        self.lock = asyncio.Lock()

    async def __aenter__(self):
        await self.lock.acquire()
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        self.lock.release()

    async def query_single_json(self, query, **values):
        assert self.lock.locked()
        if "metadata" in values:
            self.metadata = json.loads(values["metadata"])
            return json.dumps({"id": chat_row(None)["id"]})
        snapshot = deepcopy(self.metadata)
        await asyncio.sleep(0)
        return json.dumps(chat_row(snapshot))


async def test_concurrent_setting_patches_merge_without_losing_unknown_metadata():
    client = MetadataClient()
    repository = EdgeDBRepository(client=client)
    await asyncio.gather(
        repository.patch_settings(-1001, {"auto_video_links": False}),
        repository.patch_settings(-1001, {"with_nsfw": True}),
    )
    assert client.metadata == {"other": {"value": 7}, "settings": {"future_option": [1, 2], "auto_video_links": False, "with_nsfw": True}}


async def test_archival_rolls_back_metadata_and_raw_record_together_on_failure():
    original = RuntimeError("synthetic database failure")
    client = JsonClient(chat_row("{}"), original)
    update = ArchivedUpdate(
        update_id=1, kind="message", handled=True, data={"update_id": 1}, chats=[ChatObservation(chat_id=-1001, type="supergroup")]
    )
    with pytest.raises(RuntimeError) as caught:
        await EdgeDBRepository(client=client).archive_update(update)
    assert caught.value is original and not client.committed
    assert len(client.calls) == 2 and all(in_transaction for _, _, in_transaction in client.calls)
    assert "BotUpdate" in client.calls[-1][0]


async def test_legacy_archive_preserves_original_envelope_without_serializing_it_to_other_backends():
    original = {"update_id": 11, "message": {"message_id": 7, "text": "LEGACY_BODY_CANARY"}}
    update = ArchivedUpdate(
        update_id=11, kind="message", handled=True, data={"update_id": 11, "message": {"message_id": 7}}, legacy_data=original
    )
    assert "LEGACY_BODY_CANARY" not in repr(update) + update.model_dump_json()
    client = JsonClient({"id": directory_row()["id"]})
    await EdgeDBRepository(client=client).archive_update(update)
    assert json.loads(client.calls[0][1]["data"]) == original


async def test_invalid_record_error_hides_raw_private_input():
    client = JsonClient({"chat_id": "PRIVATE_RECORD_CANARY"})
    with pytest.raises(RuntimeError, match="invalid record") as caught:
        await EdgeDBRepository(client=client).get_chat(1)
    assert "PRIVATE_RECORD_CANARY" not in str(caught.value)


async def test_empty_patch_only_reads_and_does_not_replace_metadata():
    client = JsonClient(chat_row({"settings": {"future_option": "retained"}}))
    result = await EdgeDBRepository(client=client).patch_settings(-1001, {})
    assert result == {"future_option": "retained"} and len(client.calls) == 1


async def test_cursor_advances_monotonically_and_delete_returns_boolean():
    client = JsonClient({"id": directory_row()["id"]}, False, True)
    repository = EdgeDBRepository(client=client)
    await repository.advance_vk_cursor(-10, -20, 9)
    assert "max({.last_post_id, <int32>$last_post_id})" in client.calls[0][0]
    assert await repository.delete_directory(-1001) is False
    assert await repository.delete_directory(-1001) is True


async def test_check_and_close_own_the_supplied_client():
    client = SimpleNamespace(query_single_json=AsyncMock(return_value="1"), aclose=AsyncMock())
    repository = EdgeDBRepository(client=client)
    await repository.check()
    await repository.close()
    client.aclose.assert_awaited_once()
