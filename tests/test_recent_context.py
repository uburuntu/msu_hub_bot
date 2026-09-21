"""Synthetic human message context; no Telegram or storage calls."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from aiogram.types import Chat, Message, User

from msu_hub_bot.telegram.recent_context import RecentMessages, RecentMessagesMiddleware


def message(identity, *, text=None, caption=None, chat=-1001, topic=7, bot=False, reply=None, **fields):
    return Message(
        message_id=identity,
        date=datetime.fromtimestamp(1_700_000_000 + identity, UTC),
        chat=Chat(id=chat, type="supergroup"),
        message_thread_id=topic,
        is_topic_message=topic is not None,
        from_user=User(id=42, is_bot=bot, first_name="Synthetic"),
        text=text,
        caption=caption,
        reply_to_message=reply,
        **fields,
    )


def test_context_is_preceding_same_topic_human_text_without_reply_duplicate():
    recent = RecentMessages()
    for index in range(1, 11):
        recent.remember(message(index, text=f"human {index}"))
    recent.remember(message(20, text="future"))
    recent.remember(message(11, text="other chat", chat=-1002))
    recent.remember(message(11, text="other topic", topic=8))
    recent.remember(message(12, text="bot", bot=True))
    recent.remember(message(13, text="anonymous", sender_chat=Chat(id=-1001, type="supergroup")))
    current = message(15, text="current", reply=message(9, text="human 9"))
    recent.remember(current)
    assert recent.before(current) == ("human 5", "human 6", "human 7", "human 8", "human 10")
    assert recent.before(current, 0) == ()
    assert recent.before(message(50, topic=None)) == ()


def test_cache_bounds_duplicates_caption_and_expiry():
    now = [0.0]
    recent = RecentMessages(capacity=2, per_topic=2, max_chars=8, ttl_seconds=100, clock=lambda: now[0])
    recent.remember(message(1, caption="caption is long"))
    recent.remember(message(2, text="two"))
    recent.remember(message(2, text="changed"))
    assert recent.before(message(3)) == ("caption ", "changed")
    recent.remember(message(3, text="three"))
    assert recent.before(message(4)) == ("changed", "three")
    recent.remember(message(1, text="other", topic=8))
    recent.remember(message(1, text="third", topic=9))
    assert recent.before(message(4)) == ()
    assert len(recent._topics) == 2
    now[0] = 100
    assert recent.before(message(4, topic=9)) == ()
    assert not recent._topics


def test_quiet_chat_context_remains_for_24_hours():
    now = [0.0]
    recent = RecentMessages(clock=lambda: now[0])
    recent.remember(message(1, text="Вчера говорили по-русски"))
    now[0] = 60 * 60
    assert recent.before(message(2)) == ("Вчера говорили по-русски",)
    now[0] = 24 * 60 * 60
    assert recent.before(message(2)) == ()


@pytest.mark.parametrize("limit", [-1, 6, True, "5"])
def test_context_limit_rejected(limit):
    with pytest.raises(ValueError):
        RecentMessages().before(message(1), limit)


async def test_middleware_records_before_handler_without_exposing_current_message():
    recent = RecentMessages()
    middleware = RecentMessagesMiddleware(recent)
    previous = message(1, text="previous")
    current = message(2, text="current")
    handler = AsyncMock(return_value="handled")
    await middleware(handler, previous, {})

    async def inspect(event, data):
        assert recent.before(event) == ("previous",)
        return "result"

    assert await middleware(inspect, current, {}) == "result"
    assert recent.before(message(3)) == ("previous", "current")
    assert "previous" not in repr(recent)
