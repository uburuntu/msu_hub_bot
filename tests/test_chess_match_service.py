"""Durable chess transitions, clocks, ratings and uncertain Telegram publication."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageCaption, EditMessageMedia, SendPhoto
from aiogram.types import CallbackQuery
from pydantic import ValidationError

from msu_hub_bot.commands.chess_play_view import PlayCallback, keyboard
from msu_hub_bot.games.chess_play import service as service_module
from msu_hub_bot.games.chess_play.records import ChatMatch, Rating, SavedMatch, elo_delta
from msu_hub_bot.games.chess_play.service import PUBLICATION_WINDOW, RESULT_WINDOW, SCOPE, ChessMatchService
from msu_hub_bot.storage.features import FeatureStore, FeatureWorker, InvalidPayload
from quiz_helpers import FeatureFixture, PNG
from telegram_helpers import RecordingSession, make_message

NOW = datetime.now(UTC)


class MatchSession(RecordingSession):
    def __init__(self):
        super().__init__()
        self.sequence = 100
        self.photo_error = None
        self.edit_error = None

    async def make_request(self, bot, method, timeout=None):
        self.methods.append(method)
        if isinstance(method, SendPhoto) and self.photo_error is not None:
            raise self.photo_error
        if isinstance(method, (EditMessageCaption, EditMessageMedia)) and self.edit_error is not None:
            raise self.edit_error
        if method.__api_method__.startswith(("send", "edit")):
            self.sequence += 1
            chat_id = method.chat_id
            fields = {
                "message_id": getattr(method, "message_id", None) or self.sequence,
                "chat": {"id": chat_id, "type": "private" if chat_id > 0 else "supergroup"},
                "from_user": {"id": bot.id, "is_bot": True, "first_name": "Test bot"},
                "message_thread_id": getattr(method, "message_thread_id", None),
                "reply_markup": getattr(method, "reply_markup", None),
            }
            if isinstance(method, SendPhoto):
                fields["photo"] = [{"file_id": "photo", "file_unique_id": "photo", "width": 900, "height": 900}]
                if method.reply_parameters is not None:
                    fields["reply_to_message"] = make_message(bot, message_id=method.reply_parameters.message_id)
            return make_message(bot, **fields)
        return True


async def open_match(service, *, chat_id=-123, user_id=42, message_id=1, thread_id=17):
    source = make_message(
        service.bot,
        message_id=message_id,
        chat={"id": chat_id, "type": "supergroup"},
        from_user={"id": user_id, "is_bot": False, "first_name": f"Player {user_id}"},
        message_thread_id=thread_id,
        is_topic_message=thread_id is not None,
    )
    await service.start(source)
    return await service.get(chat_id, service.token(service.bot.id, chat_id, message_id))


def query(service, row, *, user_id, action, value="", revision=None, **message_changes):
    game = row.value.game
    data = PlayCallback(game=game.token, revision=game.revision if revision is None else revision, action=action, value=value)
    message = make_message(
        service.bot,
        **{
            "message_id": game.message_id,
            "chat": {"id": game.chat_id, "type": "supergroup"},
            "message_thread_id": game.thread_id,
            "from_user": {"id": service.bot.id, "is_bot": True, "first_name": "Test bot"},
            "photo": [{"file_id": "photo", "file_unique_id": "photo", "width": 900, "height": 900}],
            "reply_markup": keyboard(game),
            "reply_to_message": make_message(service.bot, message_id=row.value.source_message_id),
            **message_changes,
        },
    )
    callback = CallbackQuery.model_validate(
        {
            "id": f"callback-{user_id}-{action}",
            "from": {"id": user_id, "is_bot": False, "first_name": f"Player {user_id}"},
            "chat_instance": "synthetic",
            "message": message.model_dump(mode="json"),
            "data": data.pack(),
        },
        context={"bot": service.bot},
    )
    return callback, data


async def act(service, row, *, user_id, action, value="", revision=None):
    callback, data = query(service, row, user_id=user_id, action=action, value=value, revision=revision)
    await service.callback(callback, data)


async def fresh(service, row):
    return await service.get(row.value.game.chat_id, row.value.game.token)


async def drain(rig):
    for _ in range(20):
        if not await rig.worker.run_once():
            return
    raise AssertionError("Chess worker did not become idle")


def restart(rig):
    rig.store = FeatureStore(rig.backend)
    rig.worker = FeatureWorker(rig.store)
    rig.service = ChessMatchService(rig.bot, rig.store, rig.worker)
    rig.service.clock = lambda: rig.backend.now


@pytest.fixture
async def rig(monkeypatch):
    monkeypatch.setattr(service_module, "render_match", lambda game: PNG)
    backend = FeatureFixture()
    backend.now = NOW
    bot = Bot("123456789:" + "a" * 35, session=MatchSession())
    value = SimpleNamespace(backend=backend, bot=bot)
    restart(value)
    yield value
    await bot.session.close()


async def joined(rig, **kwargs):
    row = await open_match(rig.service, **kwargs)
    await act(rig.service, row, user_id=43, action="join")
    return await fresh(rig.service, row)


async def test_creation_is_atomic_and_repeated_input_never_republishes(rig):
    row = await open_match(rig.service)
    assert row.value.publication == "bound" and row.expires_at is None
    assert row.value.game.thread_id == 17
    assert (await rig.service.chats.get(SCOPE, "-123")).value.active == row.key
    assert (await rig.service.chats.get(SCOPE, "-123")).expires_at is None
    await open_match(rig.service)
    assert sum(isinstance(method, SendPhoto) for method in rig.bot.session.methods) == 1
    assert await fresh(rig.service, row) == row


async def test_join_is_exclusive_and_updates_waiting_image_before_first_move(rig):
    row = await open_match(rig.service)
    await asyncio.gather(act(rig.service, row, user_id=43, action="join"), act(rig.service, row, user_id=44, action="join"))
    game = (await fresh(rig.service, row)).value.game
    assert game.black.user_id in {43, 44} and game.moves == []
    assert len(await rig.service.ratings.list(SCOPE)) == 2
    await drain(rig)
    assert any(isinstance(method, EditMessageMedia) for method in rig.bot.session.methods)


async def test_moves_revision_topic_and_player_authorization_survive_restart(rig):
    row = await joined(rig)
    restart(rig)
    row = await fresh(rig.service, row)
    for changes in ({"message_id": row.value.game.message_id + 1}, {"message_thread_id": 99}):
        callback, data = query(rig.service, row, user_id=42, action="move", value="e2e4", **changes)
        await rig.service.callback(callback, data)
    await act(rig.service, row, user_id=99, action="move", value="e2e4")
    await act(rig.service, row, user_id=43, action="move", value="e2e4")
    assert (await fresh(rig.service, row)).value.game.moves == []
    rig.backend.now += timedelta(seconds=10)
    await act(rig.service, row, user_id=42, action="move", value="e2e4")
    moved = await fresh(rig.service, row)
    assert moved.value.game.moves == ["e2e4"]
    assert moved.value.game.white_seconds == 595
    await act(rig.service, row, user_id=42, action="move", value="e2e4")
    assert await fresh(rig.service, moved) == moved
    assert "обновилась" in rig.bot.session.methods[-1].text


async def test_deadline_is_authoritative_even_when_callback_arrives_after_restart(rig):
    row = await joined(rig)
    restart(rig)
    rig.backend.now += timedelta(seconds=600)
    await act(rig.service, row, user_id=42, action="move", value="e2e4")
    closed = await fresh(rig.service, row)
    assert closed.value.game.result == "timeout" and closed.value.game.winner == 43
    assert closed.value.game.moves == [] and closed.value.finished_at == NOW + timedelta(seconds=600)
    assert (await rig.service.chats.get(SCOPE, "-123")).value.active is None
    await drain(rig)
    result = await fresh(rig.service, row)
    assert result.value.ratings == ((800, 784), (800, 816))
    assert result.value.rating_status == "settled"


async def test_unknown_photo_send_is_never_retried_and_reservation_expires(rig):
    rig.bot.session.photo_error = TimeoutError()
    row = await open_match(rig.service)
    assert row.value.publication == "publishing" and row.value.game.message_id is None
    restart(rig)
    rig.backend.now += PUBLICATION_WINDOW
    await drain(rig)
    result = await fresh(rig.service, row)
    assert result.value.publication == "abandoned" and result.value.rating_status == "skipped"
    assert (await rig.service.chats.get(SCOPE, "-123")).value.active is None
    assert sum(isinstance(method, SendPhoto) for method in rig.bot.session.methods) == 1


async def test_uncertain_photo_can_bind_only_its_exact_bot_board_callback(rig):
    rig.bot.session.photo_error = TimeoutError()
    row = await open_match(rig.service)
    rig.bot.session.photo_error = None
    callback, data = query(rig.service, row, user_id=43, action="join", message_id=200)
    await rig.service.callback(callback, data)
    result = await fresh(rig.service, row)
    assert result.value.publication == "bound" and result.value.game.message_id == 200
    assert result.value.game.black.user_id == 43
    assert sum(isinstance(method, SendPhoto) for method in rig.bot.session.methods) == 1


async def test_binding_is_idempotent_when_callback_already_won(rig):
    row = await open_match(rig.service)
    callback, _ = query(rig.service, row, user_id=43, action="join")
    await rig.service._bind(row, callback.message)
    assert await fresh(rig.service, row) == row


async def test_lost_commit_response_replays_exact_request_without_second_move(rig):
    row = await joined(rig)
    rig.backend.lose_after_commit = 1
    await act(rig.service, row, user_id=42, action="move", value="e2e4")
    assert (await fresh(rig.service, row)).value.game.moves == ["e2e4"]
    commits = [request for operation, request in rig.backend.calls if operation == "commit"]
    assert commits[-1] == commits[-2]


async def test_concurrent_matches_settle_additively_once_and_can_go_negative(rig):
    tx = rig.service._tx()
    tx.expect_absent("ratings", "42")
    tx.put(rig.service.ratings, "42", Rating(user_id=42, name="Player", rating=1, extension={"kept": True}))
    tx.expect_absent("ratings", "43")
    tx.put(rig.service.ratings, "43", Rating(user_id=43, name="Other player", rating=1))
    await tx.commit()
    first = await joined(rig, chat_id=-123)
    second = await joined(rig, chat_id=-456)
    assert first.value.game.white_rating == second.value.game.white_rating == 1
    await asyncio.gather(act(rig.service, first, user_id=42, action="resign"), act(rig.service, second, user_id=42, action="resign"))
    rig.backend.lose_after_commit = 1
    await drain(rig)
    # A concurrent rating CAS may use a safe worker backoff before retry.
    rig.backend.now += timedelta(seconds=30)
    await drain(rig)
    delta = elo_delta(1, 1, 0)
    player = await rig.service.ratings.get(SCOPE, "42")
    assert player.value.rating == 1 + 2 * delta < 0
    assert player.value.model_extra == {"extension": {"kept": True}}
    assert (await rig.service.ratings.get(SCOPE, "43")).value.rating == 1 - 2 * delta
    for row in (first, second):
        assert (await fresh(rig.service, row)).value.rating_status == "settled"
    await drain(rig)
    assert (await rig.service.ratings.get(SCOPE, "42")).value.rating == player.value.rating
    third = await joined(rig, chat_id=-789)
    assert third.value.game.white_rating == player.value.rating


async def test_late_settlement_rearms_held_cleanup_and_keeps_permanent_ratings(rig):
    row = await joined(rig)
    await act(rig.service, row, user_id=42, action="resign")
    # Force the cleanup to run before a delayed settlement, as after an outage.
    rig.backend.now += RESULT_WINDOW + timedelta(seconds=1)
    for identity, job in rig.backend.jobs.items():
        if identity[-1] == f"settle:{row.key}":
            job["state"] = "held"
    await drain(rig)
    cleanup = next(job for key, job in rig.backend.jobs.items() if key[-1] == f"cleanup:{row.key}")
    assert cleanup["state"] == "held"
    for identity, job in rig.backend.jobs.items():
        if identity[-1] == f"settle:{row.key}":
            job["state"] = "pending"
    await drain(rig)
    assert await fresh(rig.service, row) is None
    assert len(await rig.service.ratings.list(SCOPE)) == 2
    assert all(job["state"] in {"cancelled", "complete"} for job in rig.backend.jobs.values())


async def test_only_missing_active_target_is_repaired(rig):
    tx = rig.service._tx()
    tx.expect_absent("chats", "-123")
    tx.put(rig.service.chats, "-123", ChatMatch(active="-123:abcdef123456"), expires_at=None)
    await tx.commit()
    row = await open_match(rig.service)
    assert row.value.publication == "bound"
    identity = next(key for key in rig.backend.records if key[3] == "matches")
    rig.backend.records[identity]["payload_version"] = 999
    with pytest.raises(Exception):
        await open_match(rig.service, message_id=2)
    assert (await rig.service.chats.get(SCOPE, "-123")).value.active == row.key


async def test_rating_pages_ties_rank_and_full_scan_failure(rig):
    tx = rig.service._tx()
    for user_id in range(1, 26):
        tx.expect_absent("ratings", str(user_id))
        tx.put(rig.service.ratings, str(user_id), Rating(user_id=user_id, name=f"P{user_id}", rating=1000 if user_id <= 3 else 800))
    await tx.commit()
    page, person = await rig.service.rating(25, page=1)
    assert page.total == 25 and page.pages == 3
    assert [player.user_id for player in page.players] == list(range(11, 21))
    assert person.rank == 25
    last, _ = await rig.service.rating(999, page=999)
    assert last.page == 2 and [player.user_id for player in last.players] == list(range(21, 26))
    rig.backend.fail = True
    with pytest.raises(Exception):
        await rig.service.rating(25)


async def test_render_rejection_is_held_but_missing_board_can_finish(rig):
    row = await joined(rig)
    rig.bot.session.edit_error = TelegramBadRequest(method=EditMessageCaption(chat_id=-123, message_id=101), message="caption is too long")
    await drain(rig)
    render_job = next(job for key, job in rig.backend.jobs.items() if key[-1] == f"render:{row.key}")
    assert render_job["state"] == "held"
    assert not (await fresh(rig.service, row)).value.presentation_failed
    rig.bot.session.edit_error = TelegramBadRequest(
        method=EditMessageCaption(chat_id=-123, message_id=101), message="message to edit not found"
    )
    await act(rig.service, row, user_id=42, action="resign")
    await drain(rig)
    assert (await fresh(rig.service, row)).value.rating_status == "settled"
    assert (await fresh(rig.service, row)).value.presentation_failed


async def test_unknown_fields_survive_game_and_player_mutation(rig):
    row = await open_match(rig.service)
    value = row.value.model_copy(deep=True)
    value.__pydantic_extra__["extension"] = {"x": True}
    value.game.__pydantic_extra__["future"] = [1, 2]
    value.game.white.__pydantic_extra__["badge"] = "champion"
    tx = rig.service._tx()
    tx.expect(row)
    tx.put(rig.service.matches, row.key, value, parent="-123", status="waiting")
    await tx.commit()
    row = await fresh(rig.service, row)
    await act(rig.service, row, user_id=43, action="join")
    changed = (await fresh(rig.service, row)).value
    assert changed.model_extra == {"extension": {"x": True}}
    assert changed.game.model_extra == {"future": [1, 2]}
    assert changed.game.white.model_extra == {"badge": "champion"}


@pytest.mark.parametrize(
    "changes", [{"rating_status": "settled"}, {"rating_status": "skipped"}, {"finished_at": NOW}, {"source_message_id": True}]
)
async def test_invalid_saved_match_is_rejected_without_writes(rig, changes):
    row = await open_match(rig.service)
    with pytest.raises(ValidationError):
        SavedMatch.model_validate({**row.value.model_dump(), **changes})
    identity = next(key for key in rig.backend.records if key[3] == "matches")
    raw_changes = {key: value.isoformat() if isinstance(value, datetime) else value for key, value in changes.items()}
    rig.backend.records[identity]["payload"].update(raw_changes)
    before = len([call for call in rig.backend.calls if call[0] == "commit"])
    with pytest.raises(InvalidPayload):
        await fresh(rig.service, row)
    assert len([call for call in rig.backend.calls if call[0] == "commit"]) == before
