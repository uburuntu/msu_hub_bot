"""Source privacy, destination indexing and creation replay for paused targets."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from msu_hub_bot.community.reposts import RepostCreate, Reposts, SourcePreview
from msu_hub_bot.storage.application import APPLICATION, ApplicationDocuments
from msu_hub_bot.storage.features import Conflict, FeatureStore
from msu_hub_bot.storage.models import VkPatch
from quiz_helpers import FeatureFixture


@pytest.mark.parametrize(
    "metadata,public",
    [({"id": 10, "is_closed": 0}, True), ({"id": 10, "is_closed": 1}, False), ({"id": 10}, False), ({"id": 99, "is_closed": 0}, False)],
)
async def test_preview_only_fetches_wall_after_public_identity_is_verified(metadata, public):
    api = SimpleNamespace(
        request=AsyncMock(
            side_effect=[
                [metadata],
                {
                    "items": [
                        {"id": 1, "owner_id": -10, "text": "news"},
                        {"id": 2, "owner_id": -10, "text": "private-canary", "friends_only": 1},
                        {"id": 3, "owner_id": -10, "text": "donor-canary", "donut": {"is_donut": True}},
                    ]
                },
            ]
        )
    )
    service = Reposts(FeatureStore(FeatureFixture()), api)
    result = await service.preview(SourcePreview(source="-10", include_keywords=["NEWS"]))
    assert result["available"] is public
    assert api.request.await_count == (2 if public else 1)
    assert "private-canary" not in str(result) and "donor-canary" not in str(result)
    assert not result["automatic_posting"]
    if public:
        assert [row["id"] for row in result["posts"]] == [1] and result["posts"][0]["selected"]


async def test_private_user_token_access_does_not_make_a_closed_profile_public():
    api = SimpleNamespace(request=AsyncMock(return_value=[{"id": 10, "is_closed": True, "can_access_closed": True}]))
    result = await Reposts(FeatureStore(FeatureFixture()), api).preview(SourcePreview(source="10"))
    assert not result["available"] and not result["posts"]
    api.request.assert_awaited_once_with("users.get", user_ids="10")


async def test_repost_creation_receipt_is_one_per_unique_target_and_owner_bound():
    backend = FeatureFixture()
    service = Reposts(FeatureStore(backend))
    body = RepostCreate(request_id=uuid4(), source="-10")
    first = await service.create(42, -123, 17, body)
    for _ in range(8):
        with pytest.raises(Conflict):
            await service.create(42, -123, 17, body.model_copy(update={"request_id": uuid4()}))
    with pytest.raises(Conflict):
        await service.create(42, -123, 17, body.model_copy(update={"source": "-11"}))
    assert len(backend.records) == 2
    again = await service.create(42, -123, 17, body)
    assert again.key == first.key and again.etag == first.etag
    assert len(backend.records) == 2


async def test_legacy_writer_sets_parent_and_cursor_updates_preserve_destination_index():
    backend = FeatureFixture()
    store = FeatureStore(backend)
    documents = ApplicationDocuments(store, AsyncMock())
    await documents.upsert_vk_subscription(-10, -123, VkPatch())
    await documents.advance_vk_cursor(-10, -123, 987)
    service = Reposts(store)
    assert not await service.list(-999, None)
    rows = await service.list(-123, None)
    assert len(rows) == 1 and rows[0].parent == "chat:-123:topic:0" and rows[0].value.last_post_id == 987
    assert rows[0].value.is_suspended
    last_query = [request for action, request in backend.calls if action == "list"][-1]
    assert last_query["parent"] == "chat:-123:topic:0"
    assert (await documents.subscriptions.get(APPLICATION, rows[0].key)).value.id == rows[0].value.id
