"""Application records keep their public behavior over the feature protocol."""

import asyncio
from copy import deepcopy
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from msu_hub_bot.storage.application import APPLICATION, ApplicationDocuments, ChatPreferences, DirectoryDocument, VkDocument
from msu_hub_bot.storage.errors import RepositoryAuthError, RepositoryError, RepositoryFailure, RepositoryUnavailable
from msu_hub_bot.storage.features import Conflict, FeatureProtocolError, FeatureStore, FutureVersion, InvalidPayload
from msu_hub_bot.storage.models import ChatRecord, DirectoryCreate, DirectoryPatch, VkPatch
from quiz_helpers import FeatureFixture

NOW = datetime(2030, 1, 1, tzinfo=UTC)
CANARY = "synthetic-private-document"


def chat(**changes):
    return ChatRecord(id=UUID(int=1), created=NOW, chat_id=-100, type="supergroup", metadata={}, **changes)


def setup(*, metadata=None, backend=None):
    backend = backend or FeatureFixture()
    observed = chat() if metadata is None else ChatRecord.model_validate(chat().model_dump() | {"metadata": metadata})
    lookup = AsyncMock(return_value=observed)
    return backend, ApplicationDocuments(FeatureStore(backend), lookup), observed


def seed(backend, collection, key, payload, **changes):
    row = {
        "feature": collection.feature,
        "scope": APPLICATION.model_dump(mode="json"),
        "collection": collection.name,
        "key": key,
        "etag": str(UUID(int=1)),
        "payload_version": 1,
        "payload": deepcopy(payload),
        "parent": None,
        "status": None,
        "expires_at": None,
        "created_at": NOW.isoformat(),
        "updated_at": NOW.isoformat(),
        **changes,
    }
    backend.records[(collection.feature, "application", "global", collection.name, key)] = row
    return row


def directory_payload(chat_id=-100, **changes):
    return (
        DirectoryDocument(
            id=UUID(int=abs(chat_id)),
            created=NOW,
            chat_id=chat_id,
            name="Друзья",
            section="friends",
            is_hidden=False,
            username_alias="friends",
            members=100,
            pinned_message_id=42,
        ).model_dump(mode="json")
        | changes
    )


def vk_payload(owner_id=-200, chat_id=-100, **changes):
    return (
        VkDocument(
            id=UUID(int=2),
            created=NOW,
            owner_id=owner_id,
            chat_id=chat_id,
            last_post_id=23,
            with_reposts=True,
            with_header=True,
            is_suspended=False,
            description="Лента",
        ).model_dump(mode="json")
        | changes
    )


def writes(backend):
    return [request for operation, request in backend.calls if operation == "commit"]


async def test_settings_seed_once_with_defaults_extras_and_explicit_forever():
    backend, documents, observed = setup(metadata={"settings": {"with_nsfw": True, "future": {"note": None}}})
    expected = {
        "auto_speech_recognition": True,
        "auto_video_links": True,
        "auto_x_previews": True,
        "with_nsfw": True,
        "future": {"note": None},
    }
    assert await documents.load_settings(observed) == expected
    observed.metadata = {"settings": {"with_nsfw": None}}
    assert await documents.load_settings(observed) == expected
    assert len(writes(backend)) == 1
    request = writes(backend)[0]
    assert request["scope"] == {"key": "global", "owner": "application"}
    assert request["feature"] == "settings" and request["puts"][0]["collection"] == "chats"
    assert request["puts"][0]["key"] == "-100" and request["puts"][0]["expires_at"] is None


@pytest.mark.parametrize("metadata", [None, [], "legacy", {"settings": None}, {"settings": []}])
async def test_nonobject_legacy_metadata_seeds_only_defaults(metadata):
    _, documents, observed = setup(metadata=metadata)
    assert await documents.load_settings(observed) == ChatPreferences().model_dump()


async def test_concurrent_settings_patches_preserve_each_others_fields_and_nulls():
    backend, documents, observed = setup(metadata={"settings": {"future": {"keep": [1, None]}}})
    await documents.load_settings(observed)
    await asyncio.gather(
        documents.patch_settings(-100, {"with_nsfw": True}),
        documents.patch_settings(-100, {"auto_video_links": False}),
        documents.patch_settings(-100, {"other": None}),
    )
    assert await documents.load_settings(observed) == {
        "auto_speech_recognition": True,
        "auto_video_links": False,
        "auto_x_previews": True,
        "with_nsfw": True,
        "future": {"keep": [1, None]},
        "other": None,
    }
    assert len(writes(backend)) > 4


@pytest.mark.parametrize("enabled", [False, True])
async def test_x_previews_inherit_existing_choice_then_remain_independent(enabled):
    backend, documents, observed = setup(metadata={"settings": {"auto_video_links": enabled}})
    values = await documents.load_settings(observed)
    assert values["auto_x_previews"] is enabled
    await documents.patch_settings(-100, {"auto_video_links": not enabled})
    assert (await documents.load_settings(observed))["auto_x_previews"] is enabled
    await documents.patch_settings(-100, {"auto_x_previews": not enabled})
    assert (await documents.load_settings(observed))["auto_x_previews"] is not enabled


async def test_settings_patch_requires_observed_chat():
    backend, documents, _ = setup()
    documents.chat_lookup = AsyncMock(return_value=None)
    with pytest.raises(RepositoryError) as caught:
        await documents.patch_settings(-100, {"with_nsfw": True})
    assert caught.value.code is RepositoryFailure.REJECTED
    assert not backend.calls


async def test_directory_create_is_idempotent_and_sparse_patch_preserves_identity_extras():
    backend, documents, _ = setup()
    first = await documents.create_directory(DirectoryCreate(chat_id=-100, name="Друзья"))
    duplicate = await documents.create_directory(DirectoryCreate(chat_id=-100, name="Другое имя", is_hidden=True))
    assert first == duplicate and len(writes(backend)) == 1
    saved = next(iter(backend.records.values()))
    saved["payload"]["future"] = {"secret": CANARY}
    changed = await documents.patch_directory(-100, DirectoryPatch(username_alias=None, members=77))
    assert (changed.id, changed.created, changed.name) == (first.id, first.created, "Друзья")
    assert changed.username_alias is None and changed.members == 77 and not changed.is_hidden
    assert writes(backend)[-1]["puts"][0]["payload"]["future"] == {"secret": CANARY}
    assert await documents.get_directory(-100) == changed


async def test_missing_directory_patch_delete_do_not_create_data_and_delete_is_replayable():
    backend, documents, _ = setup()
    assert await documents.patch_directory(-100, DirectoryPatch(name="Missing")) is None
    assert await documents.get_directory(-100) is None
    assert not await documents.delete_directory(-100)
    assert not writes(backend)
    await documents.create_directory(DirectoryCreate(chat_id=-100, name="Друзья"))
    backend.lose_after_commit = 1
    assert await documents.delete_directory(-100)
    assert writes(backend)[-1] == writes(backend)[-2]
    assert not await documents.delete_directory(-100)


async def test_missing_pin_removal_preserves_fields_and_replays_uncertain_commit():
    backend, documents, _ = setup()
    original = directory_payload(future={"keep": CANARY})
    seed(backend, documents.directory, "-100", original)
    backend.lose_after_commit = 1
    result = await documents.clear_directory_pin(-100, 42)
    assert result.pinned_message_id is None
    assert result.model_dump(mode="json") == original | {"pinned_message_id": None}
    assert len(writes(backend)) == 2 and writes(backend)[0] == writes(backend)[1]
    assert writes(backend)[0]["guards"] == [{"collection": "chats", "key": "-100", "etag": str(UUID(int=1))}]
    assert not writes(backend)[0]["deletes"]
    assert writes(backend)[0]["puts"][0]["expires_at"] is None
    await documents.clear_directory_pin(-100, 42)
    assert len(writes(backend)) == 2


@pytest.mark.parametrize("replacement", [99, None])
async def test_missing_pin_removal_rechecks_id_after_concurrent_repair_or_deletion(replacement):
    class ConcurrentRepair(FeatureFixture):
        async def feature_request(self, operation, request):
            if operation == "commit" and not self.calls[-1][0] == "commit":
                identity = ("ecosystem", "application", "global", "chats", "-100")
                if replacement is None:
                    self.records.pop(identity)
                else:
                    self.records[identity]["etag"] = str(UUID(int=2))
                    self.records[identity]["payload"].update(pinned_message_id=replacement, name="Concurrent repair")
            return await super().feature_request(operation, request)

    backend, documents, _ = setup(backend=ConcurrentRepair())
    seed(backend, documents.directory, "-100", directory_payload())
    result = await documents.clear_directory_pin(-100, 42)
    if replacement is None:
        assert result is None and not backend.records
    else:
        assert result.pinned_message_id == replacement and result.name == "Concurrent repair"
    assert len(writes(backend)) == 1 and not backend.receipts


async def test_missing_pin_removal_does_not_create_directory_or_change_another_pin():
    backend, documents, _ = setup()
    assert await documents.clear_directory_pin(-100, 42) is None
    seed(backend, documents.directory, "-100", directory_payload(pinned_message_id=99))
    assert (await documents.clear_directory_pin(-100, 42)).pinned_message_id == 99
    assert not writes(backend)


async def test_directory_pages_exceed_rest_default_cap_and_restore_numeric_order():
    backend, documents, _ = setup()
    for chat_id in range(-1005, 0):
        seed(backend, documents.directory, str(chat_id), directory_payload(chat_id))
    result = await documents.list_directory()
    assert [entry.chat_id for entry in result] == list(range(-1005, 0))
    assert len(backend.calls) == 6
    assert all(request["limit"] == 200 for _, request in backend.calls)


async def test_vk_pages_restore_owner_and_chat_numeric_order():
    backend, documents, _ = setup()
    identities = [(owner, chat) for owner in (-50, -6, 20) for chat in range(-105, 0)]
    for owner_id, chat_id in identities:
        seed(backend, documents.subscriptions, f"{owner_id}:{chat_id}", vk_payload(owner_id, chat_id))
    result = await documents.list_vk_subscriptions()
    assert [(item.owner_id, item.chat_id) for item in result] == sorted(identities)
    assert len(backend.calls) == 2


async def test_vk_sparse_upsert_preserves_identity_cursor_and_explicit_null():
    backend, documents, _ = setup()
    original = seed(backend, documents.subscriptions, "-200:-100", vk_payload(future={"keep": CANARY}))
    changed = await documents.upsert_vk_subscription(-200, -100, VkPatch(description=None, with_header=False))
    assert str(changed.id) == original["payload"]["id"] and changed.created == NOW
    assert changed.last_post_id == 23 and changed.with_reposts and not changed.with_header and changed.description is None
    assert writes(backend)[0]["puts"][0]["payload"]["future"] == {"keep": CANARY}
    reset = await documents.upsert_vk_subscription(-200, -100, VkPatch(last_post_id=0))
    assert reset.last_post_id == 0 and reset.id == changed.id


async def test_vk_advances_are_monotonic_under_contention_without_creating_missing_subscriptions():
    backend, documents, _ = setup()
    await documents.advance_vk_cursor(-200, -100, 123)
    assert not writes(backend)
    created = await documents.upsert_vk_subscription(-200, -100, VkPatch())
    assert created.last_post_id == 0 and not created.with_reposts and created.with_header and created.is_suspended
    await asyncio.gather(*(documents.advance_vk_cursor(-200, -100, value) for value in [10, 100, 50, 1000, 200]))
    [updated] = await documents.list_vk_subscriptions()
    assert updated.last_post_id == 1000 and updated.id == created.id


async def test_lost_create_response_replays_exact_request_including_generated_identity():
    backend, documents, _ = setup()
    backend.lose_after_commit = 1
    result = await documents.create_directory(DirectoryCreate(chat_id=-100, name="Друзья"))
    assert len(writes(backend)) == 2 and writes(backend)[0] == writes(backend)[1]
    assert str(result.id) == writes(backend)[0]["puts"][0]["payload"]["id"]
    assert len(backend.records) == len(backend.receipts) == 1


@pytest.mark.parametrize(
    "collection,key,payload,method",
    [
        ("settings", "-100", {"with_nsfw": None}, "load_settings"),
        ("directory", "-100", {"name": CANARY}, "get_directory"),
        ("subscriptions", "-200:-100", {"description": CANARY}, "list_vk_subscriptions"),
    ],
)
@pytest.mark.parametrize("future", [False, True])
async def test_invalid_and_future_documents_are_never_replaced(collection, key, payload, method, future):
    backend, documents, observed = setup()
    seed(backend, getattr(documents, collection), key, payload, payload_version=getattr(documents, collection).version + 1 if future else 1)
    args = (observed,) if method == "load_settings" else (-100,) if method == "get_directory" else ()
    with pytest.raises(FutureVersion if future else InvalidPayload) as caught:
        await getattr(documents, method)(*args)
    assert CANARY not in str(caught.value)
    assert not writes(backend)


@pytest.mark.parametrize(
    "collection,key,payload",
    [
        ("directory", "-100", directory_payload(-101)),
        ("subscriptions", "-200:-100", vk_payload(chat_id=-101)),
    ],
)
async def test_payload_identity_must_match_feature_key(collection, key, payload):
    backend, documents, _ = setup()
    seed(backend, getattr(documents, collection), key, payload)
    with pytest.raises(FeatureProtocolError):
        if collection == "directory":
            await documents.get_directory(-100)
        else:
            await documents.advance_vk_cursor(-200, -100, 100)
    assert not writes(backend)


async def test_conflicts_retry_at_most_eight_times():
    class Contended(FeatureFixture):
        async def feature_request(self, operation, request):
            if operation == "commit":
                self.calls.append((operation, deepcopy(request)))
                return {"outcome": "conflict", "records": []}
            return await super().feature_request(operation, request)

    backend, documents, _ = setup(backend=Contended())
    with pytest.raises(Conflict):
        await documents.create_directory(DirectoryCreate(chat_id=-100, name="Друзья"))
    assert len(writes(backend)) == 8
    assert len({request["operation_id"] for request in writes(backend)}) == 8


@pytest.mark.parametrize(
    "error,retries",
    [
        (RepositoryAuthError(RepositoryFailure.DENIED), 1),
        (RepositoryUnavailable(RepositoryFailure.CLOSED), 1),
        (RepositoryUnavailable(RepositoryFailure.TIMEOUT), 2),
        (TimeoutError(), 2),
    ],
)
async def test_only_uncertain_commits_retry_and_never_rebuild_transaction(error, retries):
    class Failed(FeatureFixture):
        async def feature_request(self, operation, request):
            if operation == "commit":
                self.calls.append((operation, deepcopy(request)))
                raise error
            return await super().feature_request(operation, request)

    backend, documents, _ = setup(backend=Failed())
    with pytest.raises(type(error)):
        await documents.create_directory(DirectoryCreate(chat_id=-100, name="Друзья"))
    assert len(writes(backend)) == retries
    assert all(request == writes(backend)[0] for request in writes(backend))


@pytest.mark.parametrize("changes", [{"with_nsfw": None}, {"new": float("nan")}, {"new": object()}])
async def test_invalid_settings_patch_never_commits(changes):
    backend, documents, _ = setup()
    with pytest.raises(InvalidPayload):
        await documents.patch_settings(-100, changes)
    assert not writes(backend)


@pytest.mark.parametrize("chat_id", [True, "-100", 2**63, -(2**63) - 1])
async def test_invalid_identifiers_do_not_reach_storage(chat_id):
    backend, documents, _ = setup()
    with pytest.raises(ValueError):
        await documents.get_directory(chat_id)
    assert not backend.calls
