"""aiogram storage, durable deletion and private restore contracts without network."""

from collections import UserDict
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram.exceptions import DataNotDictLikeError, TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter
from aiogram.fsm.state import State
from aiogram.fsm.storage.base import StorageKey
from aiogram.methods import DeleteMessage
from pydantic import ValidationError

from msu_hub_bot.storage.errors import RepositoryFailure, RepositoryUnavailable
from msu_hub_bot.storage.features import Conflict, FeatureStore, FutureVersion, InvalidPayload, Job, JobHold, JobRetry
from msu_hub_bot.telegram.deletions import Deletion, MessageDeletions
from msu_hub_bot.telegram.fsm_storage import FEATURE, Conversation, ConversationKey, FeatureFSMStorage
from msu_hub_bot.telegram.state_transfer import StateSnapshot, restore_state
from test_feature_store import Backend, raw

KEY = StorageKey(bot_id=123, chat_id=-456, user_id=789, thread_id=42)
IDENTITY = ConversationKey.model_validate(asdict(KEY))
NOW = datetime.now(UTC)
PAST = datetime(2000, 1, 1, tzinfo=UTC)
FUTURE = datetime(2099, 1, 1, tzinfo=UTC)
CANARY = "private-conversation-canary"


def conversation(**changes):
    return Conversation(key=IDENTITY, **changes)


def record(value, **changes):
    if isinstance(value, Conversation):
        collection, key, scope = "conversations", value.key.record_key(), value.key.scope
    else:
        collection, key, scope = "deletions", value.key, value.scope
    return raw(feature=FEATURE, collection=collection, key=key, scope=scope.model_dump(), payload=value.model_dump(mode="json"), **changes)


def committed(value=None):
    return {"outcome": "committed", "records": [] if value is None else [record(value)]}


def storage(*responses):
    backend = Backend(*responses)
    return backend, FeatureFSMStorage(FeatureStore(backend))


async def test_missing_state_data_value_and_clear_do_not_create_empty_records():
    backend, adapter = storage(*[("get", None)] * 5)
    assert await adapter.get_state(KEY) is None
    assert await adapter.get_data(KEY) == {}
    sentinel = object()
    assert await adapter.get_value(KEY, "missing", sentinel) is sentinel
    await adapter.set_state(KEY)
    await adapter.set_data(KEY, {})
    await adapter.close()
    assert all(operation == "get" for operation, _ in backend.calls)


@pytest.mark.parametrize(
    "field,value",
    [
        ("bot_id", 987),
        ("chat_id", -987),
        ("user_id", 987),
        ("thread_id", 987),
        ("business_connection_id", "business"),
        ("destiny", "other"),
    ],
)
def test_every_aiogram_key_dimension_has_an_independent_document(field, value):
    other = ConversationKey.model_validate(asdict(replace(KEY, **{field: value})))
    assert IDENTITY.record_key() != other.record_key()
    assert other.storage_key() == replace(KEY, **{field: value})


async def test_setting_state_preserves_data_unknown_fields_and_data_expiration():
    original = conversation(state="Before", data={"value": [1]}, data_expires_at=FUTURE, future={"keep": True})
    after = original.model_copy(update={"state": "After"})
    backend, adapter = storage(("get", record(original)), ("commit", committed(after)))
    await adapter.set_state(KEY, "After")
    request = backend.calls[-1][1]
    assert request["puts"][0]["payload"] == after.model_dump(mode="json")
    assert request["guards"][0]["etag"] == record(original)["etag"]
    assert request["puts"][0]["expires_at"] is None


async def test_state_objects_match_aiogram_state_contract():
    state = State("input", group_name="Example")
    backend, adapter = storage(("get", None), ("commit", committed(conversation(state=state.state))))
    await adapter.set_state(KEY, state)
    assert backend.calls[-1][1]["puts"][0]["payload"]["state"] == state.state


async def test_clear_removes_only_target_field_and_deletes_only_empty_document():
    original = conversation(state="Before", data={"text": "Тест 😀"})
    data_only = conversation(data=original.data)
    backend, adapter = storage(
        ("get", record(original)), ("commit", committed(data_only)), ("get", record(data_only)), ("commit", committed())
    )
    await adapter.set_state(KEY, None)
    await adapter.set_data(KEY, {})
    assert backend.calls[1][1]["puts"][0]["payload"]["data"] == original.data
    assert backend.calls[3][1]["puts"] == []
    assert backend.calls[3][1]["deletes"] == [{"collection": "conversations", "key": IDENTITY.record_key()}]


async def test_clear_data_preserves_state_and_future_fields():
    original = conversation(state="Before", data={"old": 1}, data_expires_at=FUTURE, future="keep")
    after = original.model_copy(update={"data": {}, "data_expires_at": None})
    backend, adapter = storage(("get", record(original)), ("commit", committed(after)))
    await adapter.set_data(KEY, {})
    assert backend.calls[-1][1]["puts"][0]["payload"] == after.model_dump(mode="json")


async def test_atomic_update_retries_conflict_and_keeps_concurrent_fields():
    original = conversation(data={"first": 1})
    concurrent = conversation(data={"first": 1, "second": 2})
    result = conversation(data={"first": 1, "second": 2, "third": [3]})
    backend, adapter = storage(
        ("get", record(original)), ("commit", {"outcome": "conflict"}), ("get", record(concurrent)), ("commit", committed(result))
    )
    supplied = {"third": [3]}
    returned = await adapter.update_data(KEY, supplied)
    assert returned == result.data
    returned["third"].append(4)
    assert supplied == {"third": [3]}
    assert backend.calls[-1][1]["puts"][0]["payload"]["data"] == result.data
    assert backend.calls[1][1]["operation_id"] != backend.calls[3][1]["operation_id"]


async def test_conflicts_are_bounded_and_propagate():
    backend, adapter = storage(*[("get", None), ("commit", {"outcome": "conflict"})] * 12)
    with pytest.raises(Conflict):
        await adapter.update_data(KEY, {"value": 1})
    assert len(backend.calls) == 24


async def test_reads_return_deep_independent_json_and_default_values():
    original = conversation(data={"nested": {"list": [1]}})
    source = record(original)
    _, adapter = storage(("get", source), ("get", source), ("get", source))
    result = await adapter.get_data(KEY)
    result["nested"]["list"].append(2)
    assert await adapter.get_value(KEY, "nested") == {"list": [1]}
    assert await adapter.get_value(KEY, "absent", "default") == "default"
    assert source["payload"]["data"]["nested"]["list"] == [1]


@pytest.mark.parametrize("method", ["set_data", "update_data"])
async def test_aiogram_rejects_non_dict_mappings_before_storage_io(method):
    backend, adapter = storage()
    with pytest.raises(DataNotDictLikeError):
        await getattr(adapter, method)(KEY, UserDict({"a": 1}))
    assert backend.calls == []


@pytest.mark.parametrize("damage,error", [("future", FutureVersion), ("invalid", InvalidPayload), ("identity", InvalidPayload)])
async def test_future_invalid_and_mismatched_payloads_are_never_overwritten(damage, error):
    source = record(conversation(data={"text": CANARY}))
    if damage == "future":
        source["payload_version"] = 2
    elif damage == "invalid":
        source["payload"]["data"] = CANARY
    else:
        source["payload"]["key"]["thread_id"] = 99
    backend, adapter = storage(("get", source))
    with pytest.raises(error) as caught:
        await adapter.set_state(KEY, None)
    assert CANARY not in str(caught.value)
    assert [op for op, _ in backend.calls] == ["get"]


@pytest.mark.parametrize("value", [object(), float("nan"), {"too_large": "x" * 65536}])
async def test_non_json_or_oversized_data_cannot_be_committed(value):
    backend, adapter = storage(("get", None))
    with pytest.raises(InvalidPayload):
        await adapter.set_data(KEY, {"value": value})
    assert [op for op, _ in backend.calls] == ["get"]


async def test_separate_imported_expirations_do_not_resurrect_old_data():
    original = conversation(state="Keep", data={"expired": 1}, state_expires_at=FUTURE, data_expires_at=PAST)
    after = original.model_copy(update={"data": {"new": 2}, "data_expires_at": None})
    backend, adapter = storage(
        ("get", record(original)), ("get", record(original)), ("get", record(original)), ("commit", committed(after))
    )
    assert await adapter.get_state(KEY) == "Keep"
    assert await adapter.get_data(KEY) == {}
    assert await adapter.update_data(KEY, {"new": 2}) == {"new": 2}
    assert backend.calls[-1][1]["puts"][0]["expires_at"] is None
    assert conversation(state="Expired", state_expires_at=PAST).current_state() is None
    assert conversation(state="Until", state_expires_at=FUTURE, data={"earlier": 1}, data_expires_at=PAST).expiry() == FUTURE


def deletion(**changes):
    return Deletion(bot_id=KEY.bot_id, chat_id=KEY.chat_id, message_id=11, run_at=PAST, **changes)


def deletion_service(*responses):
    backend = Backend(*responses)
    bot = SimpleNamespace(id=KEY.bot_id, delete_message=AsyncMock(return_value=True))
    worker = Mock()
    service = MessageDeletions(FeatureStore(backend), bot, worker)
    worker.register.assert_called_once_with(FEATURE, "delete_message", service._execute, max_attempts=10000)
    value = deletion()
    job = Job(
        feature=FEATURE,
        scope=value.scope,
        key=value.key,
        kind="delete_message",
        record={"collection": "deletions", "key": value.key},
        generation=2,
        lease_token="00000000-0000-0000-0000-000000000001",
        run_at=PAST,
        attempts=1,
        retry_until=None,
    )
    context = SimpleNamespace(job=job, current=AsyncMock(return_value=True), status=AsyncMock(return_value=True))
    return backend, service, bot, context


async def test_deletions_are_guarded_durable_jobs_with_infinite_pending_retention():
    backend, service, _, _ = deletion_service(("get", None), ("commit", committed(deletion())))
    await service.mark_message_to_delete_raw(KEY.chat_id, 11, after=60)
    request = backend.calls[-1][1]
    assert request["guards"] == [{"collection": "deletions", "key": "-456:11", "etag": None}]
    assert request["puts"][0]["expires_at"] is None
    assert request["jobs"][0]["key"] == "-456:11"
    assert request["jobs"][0]["record"] == {"collection": "deletions", "key": "-456:11"}
    assert datetime.fromisoformat(request["jobs"][0]["run_at"]) > NOW


async def test_reschedule_keeps_unknown_fields_and_conflicts_do_not_replace_other_identity():
    original = deletion(extra="keep")
    backend, service, _, _ = deletion_service(("get", record(original)), ("commit", committed(original)))
    await service.mark_message_to_delete_raw(KEY.chat_id, 11, after=0)
    assert backend.calls[-1][1]["puts"][0]["payload"]["extra"] == "keep"
    bad = record(original)
    bad["payload"]["message_id"] = 12
    backend, service, _, _ = deletion_service(("get", bad))
    with pytest.raises(InvalidPayload):
        await service.mark_message_to_delete_raw(KEY.chat_id, 11, after=0)
    assert len(backend.calls) == 1


@pytest.mark.parametrize("after", [-1, True, 10 * 24 * 60 * 60 + 1])
async def test_invalid_deletion_delay_does_not_write(after):
    backend, service, _, _ = deletion_service()
    with pytest.raises(ValueError):
        await service.mark_message_to_delete_raw(KEY.chat_id, 11, after)
    assert backend.calls == []


@pytest.mark.parametrize(
    "error",
    [
        None,
        TelegramForbiddenError(DeleteMessage(chat_id=-456, message_id=11), "forbidden"),
        TelegramBadRequest(DeleteMessage(chat_id=-456, message_id=11), "Bad Request: message to delete not found"),
        TelegramBadRequest(DeleteMessage(chat_id=-456, message_id=11), "Bad Request: message can't be deleted"),
        TelegramBadRequest(DeleteMessage(chat_id=-456, message_id=11), "Bad Request: chat not found"),
    ],
)
async def test_success_and_terminal_delete_errors_complete_with_bounded_audit_retention(error):
    value = deletion()
    backend, service, bot, context = deletion_service(("get", record(value)), ("commit", committed(deletion(complete=True))))
    bot.delete_message.side_effect = error
    await service._execute(context)
    bot.delete_message.assert_awaited_once_with(KEY.chat_id, 11)
    put = backend.calls[-1][1]["puts"][0]
    assert put["payload"]["complete"] is True
    assert NOW + timedelta(days=6) < datetime.fromisoformat(put["expires_at"]) < NOW + timedelta(days=8)


@pytest.mark.parametrize(
    "error,expected",
    [
        (TelegramNetworkError(DeleteMessage(chat_id=-456, message_id=11), "uncertain"), JobRetry),
        (TimeoutError(), JobRetry),
        (TelegramBadRequest(DeleteMessage(chat_id=-456, message_id=11), "unclassified"), JobHold),
    ],
)
async def test_uncertain_deletion_retries_and_unclassified_failure_holds(error, expected):
    backend, service, bot, context = deletion_service(("get", record(deletion())))
    bot.delete_message.side_effect = error
    with pytest.raises(expected):
        await service._execute(context)
    assert [op for op, _ in backend.calls] == ["get"]


async def test_retry_after_respects_telegram_delay_without_terminalizing_record():
    _, service, bot, context = deletion_service(("get", record(deletion())))
    bot.delete_message.side_effect = TelegramRetryAfter(DeleteMessage(chat_id=-456, message_id=11), "flood", retry_after=123)
    await service._execute(context)
    context.status.assert_awaited_once()
    assert context.status.call_args.args == ("retry",)
    assert context.status.call_args.kwargs["run_at"] > NOW + timedelta(seconds=122)


@pytest.mark.parametrize("phase", ["read", "complete"])
async def test_database_outage_safely_retries_deletion(phase):
    unavailable = RepositoryUnavailable(RepositoryFailure.UNAVAILABLE)
    responses = [("get", unavailable)] if phase == "read" else [("get", record(deletion())), *[("commit", unavailable)] * 3]
    _, service, _, context = deletion_service(*responses)
    with pytest.raises(JobRetry):
        await service._execute(context)


async def test_obsolete_generation_and_missing_or_completed_records_do_not_delete():
    for source, current in [(None, True), (record(deletion(complete=True)), True), (record(deletion()), False)]:
        _, service, bot, context = deletion_service(("get", source))
        context.current.return_value = current
        await service._execute(context)
        bot.delete_message.assert_not_awaited()


async def test_new_schedule_is_not_acknowledged_by_old_completion():
    _, service, bot, context = deletion_service(("get", record(deletion())), ("commit", {"outcome": "conflict"}))
    await service._execute(context)
    bot.delete_message.assert_awaited_once()


def snapshot(*, conversations=None, deletions=None, **changes):
    return StateSnapshot.model_validate(
        {
            "format": "feature-state-v1",
            "bot_id": KEY.bot_id,
            "conversations": [conversation(state="Input", data={"text": CANARY}).model_dump(mode="json")]
            if conversations is None
            else conversations,
            "deletions": [] if deletions is None else deletions,
            **changes,
        }
    )


async def test_restore_dry_run_validates_then_apply_writes_conversation_and_job_atomically():
    source = snapshot(deletions=[{"chat_id": KEY.chat_id, "message_id": 11, "run_at": PAST.isoformat()}])
    backend = Backend(("get", None), ("get", None))
    result = await restore_state(FeatureStore(backend), source, bot_id=KEY.bot_id)
    assert (result.conversations, result.deletions, result.existing) == (1, 1, 0)
    backend = Backend(("get", None), ("get", None), ("commit", committed(source.conversations[0])), ("commit", committed(deletion())))
    await restore_state(FeatureStore(backend), source, bot_id=KEY.bot_id, apply=True)
    assert backend.calls[-1][1]["jobs"][0]["run_at"] == PAST.isoformat()
    assert all(call[1]["guards"][0]["etag"] is None for call in backend.calls if call[0] == "commit")


async def test_restore_repeat_skips_identical_documents_without_rescheduling_jobs():
    source = snapshot(deletions=[{"chat_id": KEY.chat_id, "message_id": 11, "run_at": PAST.isoformat()}])
    backend = Backend(("get", record(source.conversations[0])), ("get", record(deletion())))
    result = await restore_state(FeatureStore(backend), source, bot_id=KEY.bot_id, apply=True)
    assert result.existing == 2
    assert [op for op, _ in backend.calls] == ["get", "get"]


async def test_restore_preflight_finds_later_conflict_before_first_write():
    source = snapshot(deletions=[{"chat_id": KEY.chat_id, "message_id": 11, "run_at": PAST.isoformat()}])
    backend = Backend(("get", None), ("get", record(deletion(complete=True))))
    with pytest.raises(Conflict):
        await restore_state(FeatureStore(backend), source, bot_id=KEY.bot_id, apply=True)
    assert [op for op, _ in backend.calls] == ["get", "get"]


async def test_restore_never_targets_another_bot():
    backend = Backend()
    with pytest.raises(ValueError):
        await restore_state(FeatureStore(backend), snapshot(), bot_id=999)
    assert backend.calls == []


def test_snapshot_rejects_duplicate_and_foreign_identities_and_naive_timestamps():
    duplicate = conversation().model_dump(mode="json")
    with pytest.raises(ValidationError):
        snapshot(conversations=[duplicate, deepcopy(duplicate)])
    duplicate["key"]["bot_id"] = 999
    with pytest.raises(ValidationError):
        snapshot(conversations=[duplicate])
    with pytest.raises(ValidationError):
        snapshot(deletions=[{"chat_id": -456, "message_id": 11, "run_at": "2030-01-01T00:00:00"}])


async def test_restore_preserves_separate_field_expiration_and_unknown_fields():
    value = conversation(state="Input", data={"text": CANARY}, state_expires_at=FUTURE, data_expires_at=PAST, extra="keep")
    source = snapshot(conversations=[value.model_dump(mode="json")])
    backend = Backend(("get", None), ("commit", committed(value)))
    await restore_state(FeatureStore(backend), source, bot_id=KEY.bot_id, apply=True)
    value.prune_expired()
    assert backend.calls[-1][1]["puts"][0]["payload"] == value.model_dump(mode="json")
    assert backend.calls[-1][1]["puts"][0]["expires_at"] is None


@pytest.mark.parametrize("expired_field,method,argument", [("data", "set_state", None), ("state", "set_data", {})])
async def test_clearing_last_live_field_prunes_expired_payload_and_deletes_record(expired_field, method, argument):
    original = conversation(state="Input", data={"old": CANARY}, **{f"{expired_field}_expires_at": PAST})
    backend, adapter = storage(("get", record(original)), ("commit", committed()))
    await getattr(adapter, method)(KEY, argument)
    assert backend.calls[-1][1]["puts"] == []
    assert backend.calls[-1][1]["deletes"] == [{"collection": "conversations", "key": IDENTITY.record_key()}]


async def test_update_does_not_retain_expired_state():
    original = conversation(state="Expired", data={"live": 1}, state_expires_at=PAST)
    result = conversation(data={"live": 1, "new": 2})
    backend, adapter = storage(("get", record(original)), ("commit", committed(result)))
    assert await adapter.update_data(KEY, {"new": 2}) == result.data
    assert backend.calls[-1][1]["puts"][0]["payload"] == result.model_dump(mode="json")


async def test_restore_skips_fully_expired_and_empty_conversations_without_io():
    source = snapshot(
        conversations=[conversation(state="Expired", data={"old": 1}, state_expires_at=PAST, data_expires_at=PAST).model_dump(mode="json")]
    )
    backend = Backend()
    result = await restore_state(FeatureStore(backend), source, bot_id=KEY.bot_id, apply=True)
    assert result.expired_or_empty == 1 and result.conversations == 0
    assert backend.calls == []


async def test_uncertain_commit_reuses_frozen_operation_instead_of_creating_a_new_write():
    value = conversation(data={"text": "Synthetic"})
    backend, adapter = storage(("get", None), ("commit", RepositoryUnavailable(RepositoryFailure.TIMEOUT)), ("commit", committed(value)))
    await adapter.set_data(KEY, value.data)
    assert backend.calls[1][1] == backend.calls[2][1]


def test_snapshot_accepts_a_bounded_stdin_pipe_without_host_permission_changes():
    import subprocess
    import sys

    source = snapshot()
    code = "from pathlib import Path; from msu_hub_bot.telegram.state_transfer import StateSnapshot; x=StateSnapshot.read(Path('/dev/stdin')); print(len(x.conversations), len(x.deletions))"
    result = subprocess.run(
        [sys.executable, "-c", code], input=source.model_dump_json(), capture_output=True, text=True, timeout=30, check=True
    )
    assert result.stdout.strip() == "1 0" and not result.stderr
    assert CANARY not in result.stdout
