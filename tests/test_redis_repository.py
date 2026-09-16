import asyncio
import fnmatch
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.fsm.storage.base import DefaultKeyBuilder, StorageKey
from aiogram.methods import DeleteMessage

from common.tg.runtime import Supervisor
from common.tg.storage import RedisStorage, reset_legacy_fsm, reset_v3_fsm


class MemoryRedis:
    def __init__(self):
        self.values = {}
        self.hashes = {}
        self.deleted = []

    async def hset(self, key, field=None, value=None, mapping=None):
        values = self.hashes.setdefault(key, {})
        updated = mapping if mapping is not None else {field: value}
        added = sum(name not in values for name in updated)
        values.update(updated)
        return added

    async def hget(self, key, field):
        return self.hashes.get(key, {}).get(field)

    async def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    async def hdel(self, key, field):
        values = self.hashes.get(key, {})
        present = field in values
        values.pop(field, None)
        return int(present)

    async def eval(self, script, count, key, field, expected):
        assert count == 1
        assert "redis.call('HGET'" in script and "redis.call('HDEL'" in script
        return await self.hdel(key, field) if await self.hget(key, field) == expected else 0

    async def scan_iter(self, match, count):
        for key in list(self.values):
            if fnmatch.fnmatchcase(key, match):
                yield key

    async def delete(self, *keys):
        self.deleted.extend(keys)
        count = sum(key in self.values for key in keys)
        for key in keys:
            self.values.pop(key, None)
        return count


@pytest.fixture
def repository(monkeypatch):
    monkeypatch.setattr("common.tg.storage.time.time", lambda: 1_000)
    client = MemoryRedis()
    supervisor = Supervisor()
    return RedisStorage(client, prefix="hub", supervisor=supervisor), client, supervisor


async def settle(supervisor):
    async with asyncio.timeout(1):
        while supervisor.job_count:
            await asyncio.sleep(0)
    await asyncio.sleep(0)


async def test_repository_keeps_keys_client_ownership_and_due_window(repository):
    store, client, supervisor = repository
    assert await store.redis() is client
    await store.mark_message_to_delete_raw(-101, 202, 30)
    assert client.hashes == {"hub:bot:to_delete": {"-101_202": "1030"}}
    await store.set_config("checkpoint", "unchanged")
    assert client.hashes["hub:global:config"]["checkpoint"] == "unchanged"
    client.hashes["hub:bot:to_delete"] = {"-101_202": "1060", "bad-field": "0", "-101_303": "invalid"}
    bot = SimpleNamespace(delete_message=AsyncMock())
    await store.process_messages_to_delete(bot)
    assert supervisor.job_count == 0
    bot.delete_message.assert_not_called()


async def test_due_deletion_is_owned_and_duplicate_producer_passes_do_not_repeat(repository):
    store, client, supervisor = repository
    client.hashes["hub:bot:to_delete"] = {"-101_202": "900"}
    bot = SimpleNamespace(delete_message=AsyncMock())
    await asyncio.gather(store.process_messages_to_delete(bot), store.process_messages_to_delete(bot))
    await settle(supervisor)
    bot.delete_message.assert_awaited_once_with(-101, 202)
    assert client.hashes["hub:bot:to_delete"] == {}


async def test_reschedule_before_execution_prevents_old_early_delete(repository):
    store, client, supervisor = repository
    client.hashes["hub:bot:to_delete"] = {"-101_202": "900"}
    bot = SimpleNamespace(delete_message=AsyncMock())
    await store.process_messages_to_delete(bot)
    client.hashes["hub:bot:to_delete"]["-101_202"] = "2000"
    await settle(supervisor)
    bot.delete_message.assert_not_called()
    assert client.hashes["hub:bot:to_delete"]["-101_202"] == "2000"


async def test_reschedule_during_api_call_survives_conditional_hash_removal(repository):
    store, client, supervisor = repository
    client.hashes["hub:bot:to_delete"] = {"-101_202": "900"}

    async def delete(chat_id, message_id):
        client.hashes["hub:bot:to_delete"]["-101_202"] = "2000"

    await store.process_messages_to_delete(SimpleNamespace(delete_message=delete))
    await settle(supervisor)
    assert client.hashes["hub:bot:to_delete"]["-101_202"] == "2000"


async def test_transient_failure_retries_but_missing_message_is_retired(repository):
    store, client, supervisor = repository
    key = "hub:bot:to_delete"
    client.hashes[key] = {"-101_202": "900"}
    method = DeleteMessage(chat_id=-101, message_id=202)
    bot = SimpleNamespace(delete_message=AsyncMock(side_effect=TelegramNetworkError(method=method, message="synthetic")))
    await store.process_messages_to_delete(bot)
    await settle(supervisor)
    assert client.hashes[key]["-101_202"] == "900"
    bot.delete_message.side_effect = TelegramBadRequest(method=method, message="not found")
    await store.process_messages_to_delete(bot)
    await settle(supervisor)
    assert not client.hashes[key]


async def test_cancelled_deletion_preserves_durable_record(repository):
    store, client, supervisor = repository
    client.hashes["hub:bot:to_delete"] = {"-101_202": "1059"}
    bot = SimpleNamespace(delete_message=AsyncMock())
    await store.process_messages_to_delete(bot)
    result = await supervisor.drain(0.05, cancel_timeout=0.04)
    assert result.cancelled_jobs == 1
    assert client.hashes["hub:bot:to_delete"]["-101_202"] == "1059"
    bot.delete_message.assert_not_called()


async def test_legacy_reset_matches_only_owned_state_and_data():
    client = MemoryRedis()
    owned = {"hub:-101:12:state", "hub:-101:12:data", "hub:12:12:state"}
    protected = {
        "hub:global:config",
        "hub:bot:to_delete",
        "hub:-101:12:bucket",
        "hub:checkpoint",
        "msu_hub:geoguess:-101:scores",
        "msu_hub:geoguess:-101:scores:names",
        "other:-101:12:state",
        "hub:-101:12:state:extra",
        "hub:chat:12:state",
        "hub:fsm3:123:-101:2:12:default:state",
    }
    client.values = dict.fromkeys(owned | protected, "synthetic")
    assert await reset_legacy_fsm(client, prefix="hub") == len(owned)
    assert set(client.values) == protected
    assert await reset_legacy_fsm(client, prefix="hub") == 0


async def test_v3_reset_matches_real_key_builder_and_leaves_other_records():
    client = MemoryRedis()
    builder = DefaultKeyBuilder(prefix="hub:fsm3", with_bot_id=True, with_destiny=True)
    owned = {
        builder.build(StorageKey(bot_id=123, chat_id=-101, user_id=12, thread_id=topic), part)
        for topic in (None, 2, 3)
        for part in ("state", "data")
    }
    protected = {"hub:-101:12:state", "hub:bot:to_delete", "hub:fsm3:123:-101:2:12:other:state", "other:fsm3:123:-101:12:default:state"}
    client.values = dict.fromkeys(owned | protected, "synthetic")
    assert await reset_v3_fsm(client, prefix="hub") == len(owned)
    assert set(client.values) == protected


@pytest.mark.parametrize("operation", [reset_legacy_fsm, reset_v3_fsm])
@pytest.mark.parametrize("prefix", ["", "*", "hub?", "hub[1]", "hub\\"])
async def test_reset_rejects_ambiguous_namespace(operation, prefix):
    client = MemoryRedis()
    with pytest.raises(ValueError):
        await operation(client, prefix=prefix)
    assert client.deleted == []
