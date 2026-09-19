"""Durable raffle membership, one committed draw and recoverable Telegram cards."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from aiogram.types import CallbackQuery, Message
from pydantic import ValidationError

from msu_hub_bot.commands.raffle import Raffle, RaffleCallback, keyboard, valid_origin, view
from msu_hub_bot.games import raffle as module
from msu_hub_bot.games.raffle import PAGE_SIZE, Person, RETENTION, RaffleError, RaffleStore
from msu_hub_bot.storage.features import FeatureStore
from quiz_helpers import FeatureFixture
from telegram_helpers import RecordingSession, make_message


class RaffleSession(RecordingSession):
    def __init__(self, now):
        super().__init__()
        self.now = now
        self.cards = {}
        self.sequence = 100
        self.send_error = None
        self.edit_error = None

    async def make_request(self, bot, method, timeout=None):
        self.methods.append(method)
        if isinstance(method, SendMessage):
            self.sequence += 1
            message = make_message(
                bot,
                message_id=self.sequence,
                date=self.now,
                chat={"id": method.chat_id, "type": "supergroup"},
                from_user={"id": bot.id, "is_bot": True, "first_name": "Test bot"},
                text=method.text,
                entities=method.entities,
                reply_markup=method.reply_markup,
                message_thread_id=method.message_thread_id,
                is_topic_message=method.message_thread_id is not None,
                reply_to_message=make_message(bot, message_id=method.reply_parameters.message_id) if method.reply_parameters else None,
            )
            self.cards[message.message_id] = message
            if self.send_error is not None:
                error, self.send_error = self.send_error, None
                raise error
            return message
        if isinstance(method, EditMessageText):
            if self.edit_error is not None:
                raise self.edit_error
            original = self.cards[method.message_id]
            result = original.model_copy(update={"text": method.text, "entities": method.entities, "reply_markup": method.reply_markup})
            self.cards[method.message_id] = result
            return result
        return True


@pytest.fixture
async def rig():
    backend = FeatureFixture()
    session = RaffleSession(backend.now)
    bot = Bot("123456789:" + "a" * 35, session=session)
    store = RaffleStore(bot.id, FeatureStore(backend), clock=lambda: backend.now)
    source = make_message(
        bot,
        date=backend.now,
        message_id=10,
        message_thread_id=7,
        is_topic_message=True,
        from_user={"id": 42, "is_bot": False, "first_name": "Организатор", "username": "creator"},
    )
    yield SimpleNamespace(backend=backend, session=session, bot=bot, store=store, source=source)
    await bot.session.close()


async def opened(rig):
    card = await Raffle.process(rig.source, rig.store)
    assert isinstance(card, Message)
    scope = rig.store.scope(rig.source.chat.id, 7)
    token = rig.store.token(rig.source.chat.id, rig.source.message_id)
    return await rig.store.require(scope, token), card


def callback(rig, card, row, *, action="reg", user_id=99, token=None, page=0, **message_changes):
    data = RaffleCallback(action=action, token=token or row.key, page=page)
    message = Message.model_validate({**card.model_dump(mode="json"), **message_changes}, context={"bot": rig.bot})
    query = CallbackQuery.model_validate(
        {
            "id": f"callback-{user_id}-{action}",
            "from": {"id": user_id, "is_bot": False, "first_name": f"Участник {user_id}", "username": f"user{user_id}"},
            "chat_instance": "synthetic",
            "message": message.model_dump(mode="json"),
            "data": data.pack(),
        },
        context={"bot": rig.bot},
    )
    return query, data


async def act(rig, card, row, *, store=None, **kwargs):
    query, data = callback(rig, card, row, **kwargs)
    await Raffle.process_cb(query, data, store or rig.store)


async def rows(collection, scope, token):
    return await collection.list(scope, parent=token, limit=200)


async def test_username_creator_is_explicit_and_each_user_gets_one_slot(rig):
    row, card = await opened(rig)
    assert row.value.creator.user_id == 42
    assert row.value.creator.username == "creator"
    await act(rig, card, row)
    await act(rig, card, row)
    current = await rig.store.require(row.scope, row.key)
    assert current.value.participants == 1
    assert len(await rows(rig.store.members, row.scope, row.key)) == 1
    assert len(await rows(rig.store.entries, row.scope, row.key)) == 1
    answers = [method.text for method in rig.session.methods if isinstance(method, AnswerCallbackQuery)]
    assert any("уже здесь" in answer for answer in answers)


async def test_concurrent_registration_across_instances_is_unique_and_contiguous(rig):
    row, _ = await opened(rig)
    second = RaffleStore(rig.bot.id, FeatureStore(rig.backend), clock=lambda: rig.backend.now)
    outcomes = await asyncio.gather(
        *(
            store.join(row.scope, row.key, Person(user_id=user_id, name=f"User {user_id}"))
            for user_id in range(1, 41)
            for store in (rig.store, second)
        )
    )
    assert sum(joined for _, joined in outcomes) == 40
    current = await rig.store.require(row.scope, row.key)
    entries = await rows(rig.store.entries, row.scope, row.key)
    assert current.value.participants == 40
    assert [entry.value.slot for entry in entries] == list(range(40))
    assert {entry.value.person.user_id for entry in entries} == set(range(1, 41))


async def test_draw_commits_before_edit_and_cannot_reroll_after_restart(rig, monkeypatch):
    row, card = await opened(rig)
    await rig.store.join(row.scope, row.key, Person(user_id=99, name="Победитель <script>😀"))
    await rig.store.join(row.scope, row.key, Person(user_id=100, name="Второй"))
    choose = []
    monkeypatch.setattr(module.secrets, "randbelow", lambda count: choose.append(count) or 0)
    rig.session.edit_error = TimeoutError("synthetic ambiguous edit")
    await act(rig, card, row, action="winner", user_id=42)
    current = await rig.store.require(row.scope, row.key)
    assert current.value.winner.user_id == 99
    assert current.value.status == "finished"
    restarted = RaffleStore(rig.bot.id, FeatureStore(rig.backend), clock=lambda: rig.backend.now)
    rig.session.edit_error = None
    await act(rig, card, row, action="winner", user_id=42, store=restarted)
    assert choose == [2]
    assert (await restarted.require(row.scope, row.key)).value.winner.user_id == 99
    final = rig.session.cards[card.message_id]
    assert "Победитель <script>😀" in final.text
    assert "👑 Победитель:" in final.text
    edit = next(method for method in reversed(rig.session.methods) if isinstance(method, EditMessageText))
    assert edit.parse_mode is None
    assert not any(entity.type == "text_link" and "script" in (entity.url or "") for entity in edit.entities)


async def test_lost_commit_retries_the_frozen_draw_without_choosing_again(rig, monkeypatch):
    row, _ = await opened(rig)
    await rig.store.join(row.scope, row.key, Person(user_id=99, name="Участник"))
    chosen = []
    monkeypatch.setattr(module.secrets, "randbelow", lambda count: chosen.append(count) or 0)
    rig.backend.lose_after_commit = 1
    result = await rig.store.draw(row.scope, row.key, 42)
    commits = [request for operation, request in rig.backend.calls if operation == "commit"]
    assert commits[-1] == commits[-2]
    assert result.value.winner.user_id == 99
    assert chosen == [1]


async def test_draw_and_join_race_preserves_frozen_membership_and_one_winner(rig):
    row, _ = await opened(rig)
    await rig.store.join(row.scope, row.key, Person(user_id=99, name="Первый"))
    second = RaffleStore(rig.bot.id, FeatureStore(rig.backend), clock=lambda: rig.backend.now)
    outcomes = await asyncio.gather(
        rig.store.draw(row.scope, row.key, 42),
        second.draw(row.scope, row.key, 42),
        second.join(row.scope, row.key, Person(user_id=100, name="Второй")),
        return_exceptions=True,
    )
    assert all(not isinstance(value, Exception) or isinstance(value, RaffleError) for value in outcomes)
    current = await rig.store.require(row.scope, row.key)
    entries = await rows(rig.store.entries, row.scope, row.key)
    assert current.value.participants == len(entries)
    assert current.value.winner.user_id in {entry.value.person.user_id for entry in entries}
    winners = [value.value.winner.user_id for value in outcomes[:2]]
    assert winners == [current.value.winner.user_id] * 2
    with pytest.raises(RaffleError, match="Победитель уже выбран"):
        await rig.store.join(row.scope, row.key, Person(user_id=101, name="Опоздавший"))


async def test_creator_is_not_derived_from_message_entities(rig):
    row, card = await opened(rig)
    await rig.store.join(row.scope, row.key, Person(user_id=99, name="Другой"))
    await act(rig, card, row, action="winner", user_id=99, text="Конкурс от Другого", entities=[])
    assert (await rig.store.require(row.scope, row.key)).value.status == "open"
    assert any(isinstance(method, AnswerCallbackQuery) and "Только создатель" in (method.text or "") for method in rig.session.methods)


@pytest.mark.parametrize(
    "changed",
    [
        {"message_id": 999},
        {"message_thread_id": 8},
        {"from_user": {"id": 17, "is_bot": False, "first_name": "Other"}},
        {"chat": {"id": -998, "type": "supergroup"}},
    ],
)
async def test_forged_callback_origin_never_changes_membership(rig, changed):
    row, card = await opened(rig)
    before = len([method for method in rig.session.methods if isinstance(method, EditMessageText)])
    await act(rig, card, row, **changed)
    assert (await rig.store.require(row.scope, row.key)).value.participants == 0
    assert len([method for method in rig.session.methods if isinstance(method, EditMessageText)]) == before


async def test_callback_from_another_raffle_card_cannot_register(rig):
    row, card = await opened(rig)
    other_source = rig.source.model_copy(update={"message_id": 11})
    await Raffle.process(other_source, rig.store)
    other = await rig.store.require(row.scope, rig.store.token(other_source.chat.id, 11))
    await act(rig, card, row, token=other.key)
    assert (await rig.store.require(other.scope, other.key)).value.participants == 0


async def test_uncertain_initial_send_recovers_from_original_card_without_resending(rig):
    rig.session.send_error = TimeoutError("synthetic send response lost")
    await Raffle.process(rig.source, rig.store)
    row = await rig.store.require(rig.store.scope(rig.source.chat.id, 7), rig.store.token(rig.source.chat.id, rig.source.message_id))
    assert row.value.message_id is None
    original = next(card for card in rig.session.cards.values() if card.reply_markup is not None)
    await Raffle.process(rig.source, rig.store)
    assert len([method for method in rig.session.methods if isinstance(method, SendMessage) and method.reply_markup is not None]) == 1
    restarted = RaffleStore(rig.bot.id, FeatureStore(rig.backend), clock=lambda: rig.backend.now)
    await act(rig, original, row, store=restarted)
    recovered = await restarted.require(row.scope, row.key)
    assert recovered.value.message_id == original.message_id
    assert recovered.value.participants == 1


async def test_unbound_card_requires_original_markup_reply_and_publication_time(rig, monkeypatch):
    monkeypatch.setattr(rig.store, "bind", AsyncMock(side_effect=TimeoutError("binding unavailable")))
    row, card = await opened(rig)
    assert row.value.message_id is None
    assert valid_origin(row, card)
    assert not valid_origin(row, card.model_copy(update={"reply_markup": None}))
    assert not valid_origin(row, card.model_copy(update={"reply_to_message": None}))
    assert not valid_origin(row, card.model_copy(update={"date": card.date + timedelta(hours=1)}))


async def test_pages_remain_bounded_and_directly_seekable_for_large_crowds(rig):
    row, _ = await opened(rig)
    for user_id in range(1, 76):
        await rig.store.join(row.scope, row.key, Person(user_id=user_id, name="😀<a>" * 50, username="u" * 32))
    current = await rig.store.require(row.scope, row.key)
    people, page, pages = await rig.store.page(current, 5)
    assert [person.user_id for person in people] == list(range(61, 73))
    assert (page, pages) == (5, 7)
    requests = [request for operation, request in rig.backend.calls if operation == "list"]
    assert requests[-1]["after"] == rig.store.entry_key(row.key, 59)
    assert requests[-1]["limit"] == PAGE_SIZE
    huge = current.value.model_copy(update={"participants": 2**31 - 1})
    text = view(huge, people, page, pages)
    assert len(text) < 4096
    assert len(text.render()[1]) < 100
    for page_index in (0, 5, 6):
        assert all(
            len(button.callback_data.encode()) <= 64 for line in keyboard(huge, page_index, pages).inline_keyboard for button in line
        )
    people, page, pages = await rig.store.page(current, 2**31 - 1)
    assert page == pages - 1
    assert len(people) == 3


async def test_expiry_closes_without_deleting_or_recreating_saved_data(rig):
    row, card = await opened(rig)
    rig.backend.now += RETENTION + timedelta(seconds=1)
    before = len(rig.backend.records)
    await act(rig, card, row)
    assert len(rig.backend.records) == before
    assert not any(isinstance(method, EditMessageText) for method in rig.session.methods)
    assert any(isinstance(method, AnswerCallbackQuery) and "закрыт" in (method.text or "") for method in rig.session.methods)


async def test_telegram_not_modified_is_safe_and_does_not_alter_membership(rig):
    row, card = await opened(rig)
    rig.session.edit_error = TelegramBadRequest(
        method=EditMessageText(text="x", chat_id=card.chat.id, message_id=card.message_id), message="message is not modified"
    )
    await act(rig, card, row, action="refresh")
    assert (await rig.store.require(row.scope, row.key)).value.participants == 0


@pytest.mark.parametrize("value", [{"token": "bad"}, {"page": -1}, {"action": "delete"}])
def test_callback_payload_rejects_unsupported_actions_and_ranges(value):
    with pytest.raises(ValidationError):
        RaffleCallback.model_validate({"action": "reg", "token": "a" * 16, **value})
