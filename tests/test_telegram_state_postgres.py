"""Conversation CAS, recovery and deletion generations on the real SQL contracts."""

import asyncio
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.methods import DeleteMessage

from msu_hub_bot.storage.errors import RepositoryFailure, RepositoryUnavailable
from msu_hub_bot.storage.features import FeatureStore, FeatureWorker, Job, JobContext
from msu_hub_bot.telegram.deletions import MessageDeletions
from msu_hub_bot.telegram.fsm_storage import FEATURE, FeatureFSMStorage
from msu_hub_bot.telegram.state_transfer import StateSnapshot, restore_state
from test_feature_postgres import call

KEY = StorageKey(bot_id=999, chat_id=-101, user_id=42, thread_id=17)


class Backend:
    def __init__(self, db):
        self.db = db

    async def feature_request(self, operation, request):
        return await asyncio.to_thread(call, self.db, operation, request)


def service(db, bot=None):
    store = FeatureStore(Backend(db))
    worker = FeatureWorker(store)
    bot = bot or SimpleNamespace(id=999, delete_message=AsyncMock(return_value=True))
    return MessageDeletions(store, bot, worker), worker, bot


async def test_conversation_restart_atomic_merges_and_topic_isolation(application_db):
    db = application_db
    initial = FeatureFSMStorage(FeatureStore(Backend(db)))
    await initial.set_state(KEY, "Code:input")
    await initial.set_data(KEY, {"code": "print(input())", "nested": ["Тест 😀"]})
    restarted = FeatureFSMStorage(FeatureStore(Backend(db)))
    assert await restarted.get_state(KEY) == "Code:input"
    await asyncio.gather(*(restarted.update_data(KEY, {f"field_{number}": number}) for number in range(8)))
    assert await restarted.get_data(KEY) == {"code": "print(input())", "nested": ["Тест 😀"], **{f"field_{n}": n for n in range(8)}}
    other = replace(KEY, thread_id=18)
    await restarted.set_state(other, "Sticker:name")
    assert await restarted.get_state(KEY) == "Code:input"
    assert await restarted.get_state(other) == "Sticker:name"
    await FSMContext(restarted, KEY).clear()
    assert await restarted.get_state(KEY) is None and await restarted.get_data(KEY) == {}
    assert await restarted.get_state(other) == "Sticker:name"
    assert db.value("SELECT count(*) FROM msu_hub_private.feature_records;") == 1
    assert db.value("SELECT to_jsonb(expires_at IS NULL) FROM msu_hub_private.feature_records;")


async def test_uncertain_commit_replays_same_receipt_without_losing_conversation(application_db):
    class LostResponse(Backend):
        lose = True

        async def feature_request(self, operation, request):
            result = await super().feature_request(operation, request)
            if operation == "commit" and self.lose:
                self.lose = False
                raise RepositoryUnavailable(RepositoryFailure.UNAVAILABLE)
            return result

    db = application_db
    adapter = FeatureFSMStorage(FeatureStore(LostResponse(db)))
    await adapter.set_data(KEY, {"draft": "Synthetic"})
    assert await adapter.get_data(KEY) == {"draft": "Synthetic"}
    assert db.value("SELECT count(*) FROM msu_hub_private.feature_operations;") == 1


async def test_normalized_snapshot_restores_only_live_fields_and_is_repeatable(application_db):
    db = application_db
    future = datetime.now(UTC) + timedelta(hours=1)
    past = datetime.now(UTC) - timedelta(hours=1)
    source = StateSnapshot.model_validate(
        {
            "format": "feature-state-v1",
            "bot_id": 999,
            "conversations": [
                {
                    "key": asdict(KEY),
                    "state": "Code:input",
                    "data": {"expired": "private synthetic"},
                    "state_expires_at": future,
                    "data_expires_at": past,
                },
                {"key": asdict(replace(KEY, user_id=43)), "state": "Expired", "state_expires_at": past},
            ],
            "deletions": [{"chat_id": -101, "message_id": 50, "run_at": past}],
        }
    )
    store = FeatureStore(Backend(db))
    result = await restore_state(store, source, bot_id=999, apply=True)
    assert (result.conversations, result.deletions, result.expired_or_empty) == (1, 1, 1)
    repeat = await restore_state(FeatureStore(Backend(db)), source, bot_id=999, apply=True)
    assert repeat.existing == 2 and repeat.expired_or_empty == 1
    adapter = FeatureFSMStorage(store)
    assert await adapter.get_state(KEY) == "Code:input"
    assert await adapter.get_data(KEY) == {}
    assert db.value("SELECT payload->'data' FROM msu_hub_private.feature_records WHERE collection='conversations';") == {}
    assert db.value("SELECT count(*) FROM msu_hub_private.feature_jobs;") == 1
    assert db.value("SELECT generation FROM msu_hub_private.feature_jobs;") == 1


async def test_deletion_restart_retries_uncertain_network_outcome_and_finishes_missing_message(application_db):
    db = application_db
    deletions, _, _ = service(db)
    await deletions.mark_message_to_delete_raw(-101, 50, 0)
    restarted, worker, bot = service(db)
    bot.delete_message.side_effect = TelegramNetworkError(DeleteMessage(chat_id=-101, message_id=50), "synthetic uncertain request")
    assert await worker.run_once() == 1
    assert db.value("SELECT to_jsonb(state) FROM msu_hub_private.feature_jobs;") == "pending"
    assert not db.value("SELECT payload->'complete' FROM msu_hub_private.feature_records;")
    db.run("UPDATE msu_hub_private.feature_jobs SET run_at=now()-interval '1 second';")
    final, worker, bot = service(db)
    bot.delete_message.side_effect = TelegramBadRequest(
        DeleteMessage(chat_id=-101, message_id=50), "Bad Request: message to delete not found"
    )
    assert await worker.run_once() == 1
    assert await worker.run_once() == 0
    assert db.value("SELECT to_jsonb(state) FROM msu_hub_private.feature_jobs;") == "complete"
    assert db.value("SELECT payload->'complete' FROM msu_hub_private.feature_records;")
    assert db.value(
        "SELECT to_jsonb(expires_at BETWEEN now()+interval '6 days' AND now()+interval '8 days') FROM msu_hub_private.feature_records;"
    )


async def test_rescheduling_during_inflight_delete_never_completes_replacement(application_db):
    db = application_db
    deletions, worker, bot = service(db)
    await deletions.mark_message_to_delete_raw(-101, 50, 0)

    async def reschedule(chat_id, message_id):
        await deletions.mark_message_to_delete_raw(chat_id, message_id, 3600)
        return True

    bot.delete_message.side_effect = reschedule
    assert await worker.run_once() == 1
    assert db.value("SELECT to_jsonb(state) FROM msu_hub_private.feature_jobs;") == "pending"
    assert db.value("SELECT generation FROM msu_hub_private.feature_jobs;") == 2
    assert not db.value("SELECT payload->'complete' FROM msu_hub_private.feature_records;")
    assert await worker.run_once() == 0


async def test_superseded_claim_cannot_delete_or_acknowledge_new_generation(application_db):
    db = application_db
    deletions, worker, bot = service(db)
    await deletions.mark_message_to_delete_raw(-101, 50, 0)
    claimed = await deletions.store.backend.feature_request(
        "claim_jobs", {"handlers": [{"feature": FEATURE, "kind": "delete_message"}], "limit": 1, "lease_seconds": 60}
    )
    context = JobContext(deletions.store, Job.model_validate(claimed[0]))
    await deletions.mark_message_to_delete_raw(-101, 50, 3600)
    await deletions._execute(context)
    bot.delete_message.assert_not_awaited()
    assert not await context.status("complete")
    assert db.value("SELECT to_jsonb(state) FROM msu_hub_private.feature_jobs;") == "pending"
    assert db.value("SELECT generation FROM msu_hub_private.feature_jobs;") == 2
