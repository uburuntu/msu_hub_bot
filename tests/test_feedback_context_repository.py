"""The feedback read RPC has a narrow, validated wire response."""

from datetime import UTC, datetime

import pytest

from msu_hub_bot.storage.errors import RepositoryProtocolError
from test_supabase_repository import Response, configured as configured, token

NOW = datetime(2026, 9, 21, 12, tzinfo=UTC)


def record(**changes):
    return {
        "chat_id": -123,
        "message_id": 9,
        "sent_at": NOW.isoformat(),
        "thread_id": None,
        "author_id": 42,
        "author_kind": "user",
        "author_name": "Synthetic",
        "text": "Caption",
        "media_kind": "photo",
        "truncated": False,
        **changes,
    }


async def test_recent_context_rpc_is_authenticated_bounded_and_typed(configured):
    repository, session = configured([Response(token()), Response([record()])])
    try:
        result = await repository.recent_feedback_messages(-123, thread_id=None, before=NOW, before_message_id=100)
        assert len(result) == 1 and result[0].media_kind == "photo"
        url, request = session.calls[-1]
        assert str(url).endswith("/rest/v1/rpc/recent_feedback_messages_v1")
        assert request["headers"]["Authorization"] == "Bearer access-one"
        assert request["json"] == {"p_chat_id": -123, "p_thread_id": None, "p_before": NOW.isoformat(), "p_before_message_id": 100}
    finally:
        await repository.close()


@pytest.mark.parametrize(
    "payload",
    [[record(data={"text": "private"})], [record(text="x" * 801)], [record(media_kind="file_id")], [record()] * 6, {"messages": []}],
)
async def test_unreviewed_payload_shapes_are_rejected_without_raw_data(configured, payload):
    repository, _ = configured([Response(token()), Response(payload)])
    try:
        with pytest.raises(RepositoryProtocolError, match="invalid_response"):
            await repository.recent_feedback_messages(-123, thread_id=7, before=NOW, before_message_id=100)
    finally:
        await repository.close()


@pytest.mark.parametrize(
    "changes",
    [
        {"chat_id": 0},
        {"chat_id": True},
        {"thread_id": 0},
        {"thread_id": -1},
        {"before_message_id": 0},
        {"before": NOW.replace(tzinfo=None)},
    ],
)
async def test_invalid_scopes_do_not_authenticate_or_request(configured, changes):
    repository, session = configured([])
    try:
        with pytest.raises(ValueError):
            await repository.recent_feedback_messages(
                **{"chat_id": -123, "thread_id": None, "before": NOW, "before_message_id": 100, **changes}
            )
        assert session.calls == []
    finally:
        await repository.close()
