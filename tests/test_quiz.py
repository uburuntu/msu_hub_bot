"""Quiz behavior across concurrent inputs, uncertain delivery and process restarts."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import AnswerCallbackQuery, EditMessageCaption, EditMessageMedia, SendMessage, SendPhoto

from msu_hub_bot.commands import chess, geoguess
from msu_hub_bot.games import definitions
from msu_hub_bot.providers.exceptions import ExternalServiceError
from quiz_helpers import PHOTO, PNG, PUZZLE, click, edits, restart, rig as rig, score_rows, score_values, settle, start, text_of
from telegram_helpers import make_message


async def current(rig, record):
    return await rig.quiz.round(rig.feature, record.value.chat_id, record.key)


async def votes(rig, record):
    return await rig.quiz.votes(rig.feature, record.scope, record.key)


async def test_photo_has_six_hidden_choices_and_one_chat_slot(rig):
    record = await start(rig)
    await start(rig)
    photos = [method for method in rig.session.methods if isinstance(method, SendPhoto)]
    assert len(photos) == 1
    photo, state = photos[0], record.value
    assert state.phase == "active" and state.question.kind == rig.feature
    assert state.deadline_at == state.published_at + timedelta(minutes=10)
    assert photo.reply_parameters.message_id == rig.message.message_id and photo.message_thread_id == 17
    buttons = [button for row in photo.reply_markup.inline_keyboard for button in row]
    assert len(buttons) == 7 and len(set(state.question.choices)) == 6
    assert [button.callback_data for button in buttons[:-1]] == [f"{rig.feature}:{record.key}:{i}" for i in range(6)]
    assert buttons[-1].text == "Завершить задание"
    assert photo.parse_mode is None and photo.caption_entities
    if rig.feature == "chess":
        assert photo.photo.data == PNG and "Ход белых" in photo.caption
        assert not any(value in photo.caption for value in (PUZZLE.id, "lichess", "Rxc8", "f8c8", PUZZLE.fen))
    else:
        assert photo.photo == PHOTO.url and "Угадай страну" in photo.caption
        assert PHOTO.country not in photo.caption and PHOTO.city not in photo.caption
        assert "Author <name>" in photo.caption and "CC BY 3.0" in photo.caption
    assert "прошлое задание" in rig.session.methods[-1].text
    assert all(timeout == 15 for timeout in rig.session.timeouts)


async def test_votes_are_immutable_hidden_and_acknowledged_after_durable_commit(rig):
    record = await start(rig)
    answer = record.value.question.answer
    rig.backend.lose_after_commit = 1
    await click(rig, record, answer)
    await click(rig, record, (answer + 1) % 6)
    await click(rig, record, (answer + 1) % 6, user_id=43)
    await settle(rig)
    stored = await votes(rig, record)
    assert [(vote.value.user_id, vote.value.choice) for vote in stored] == [(42, answer), (43, (answer + 1) % 6)]
    assert (await current(rig, record)).value.vote_count == 2
    hidden = text_of(edits(rig)[-1])
    assert "Ответили: 2" in hidden and "User 42 <&>" in hidden and "@user_43" in hidden
    assert all(option not in hidden for option in record.value.question.choices)
    answers = [method.text for method in rig.session.methods if isinstance(method, AnswerCallbackQuery)]
    assert "Ответ принят" in answers[0] and "Изменить его нельзя" in answers[1]
    commits = [request for operation, request in rig.backend.calls if operation == "commit"]
    replay_ids = [request["operation_id"] for request in commits]
    assert len(replay_ids) > len(set(replay_ids))


async def test_finish_scores_all_players_once_with_floor_and_names(rig):
    record = await start(rig)
    answer = record.value.question.answer
    await click(rig, record, answer)
    await click(rig, record, (answer + 1) % 6, user_id=43)
    await asyncio.gather(click(rig, record, "finish"), click(rig, record, "finish"))
    await settle(rig)
    stored = await current(rig, record)
    assert stored.value.phase == "closed" and stored.value.score_status == "recorded"
    assert await score_values(rig, stored.value.score_day) == {42: 1, 43: 0}
    players = await score_rows(rig, stored.value.score_day)
    assert players[0].value.name == "User 42 <&>" and players[0].value.username == "user_42"
    assert stored.value.score_count == 2
    result = text_of(edits(rig)[-1])
    assert "Угадали 1 из 2" in result and "+1" in result and "−1" in result
    if rig.feature == "chess":
        assert "Rxc8+" in result and any(isinstance(edit, EditMessageMedia) for edit in edits(rig))
    else:
        assert "Берген" in result and "Норвегия" in result and "Источник фотографии" in result
    await click(rig, record, answer, user_id=44)
    assert len(await votes(rig, record)) == 2


async def test_cancelled_finish_callback_cannot_cancel_durable_settlement(rig, monkeypatch):
    record = await start(rig)
    await click(rig, record, record.value.question.answer)
    entered = asyncio.Event()
    original = rig.session.make_request

    async def blocked_ack(bot, method, timeout=None):
        if isinstance(method, AnswerCallbackQuery):
            entered.set()
            await asyncio.Event().wait()
        return await original(bot, method, timeout)

    monkeypatch.setattr(rig.session, "make_request", blocked_ack)
    callback = asyncio.create_task(click(rig, record, "finish"))
    await entered.wait()
    callback.cancel()
    await asyncio.gather(callback, return_exceptions=True)
    monkeypatch.setattr(rig.session, "make_request", original)
    restart(rig)
    await settle(rig)
    assert (await current(rig, record)).value.score_status == "recorded"
    assert (await current(rig, record)).value.score_status == "recorded"


@pytest.mark.parametrize("choice", ["-1", "6", "nope", "page_", "page_-1", "page_1.5", "page_١", "page_999999999"])
async def test_invalid_buttons_never_vote_or_edit(rig, choice):
    record = await start(rig)
    await click(rig, record, choice)
    await settle(rig)
    assert not await votes(rig, record) and not edits(rig)


async def test_wrong_message_and_stale_token_do_not_change_state(rig):
    record = await start(rig)
    await click(rig, record, "0", token="stale")
    wrong = rig.session.messages[record.value.message_id].model_copy(update={"message_id": 999})
    await click(rig, record, "0", message=wrong)
    await settle(rig)
    assert not await votes(rig, record) and not edits(rig)


async def test_restart_preserves_answers_options_attribution_and_deadline(rig):
    record = await start(rig)
    await click(rig, record, record.value.question.answer)
    original = (await current(rig, record)).value.model_dump()
    restart(rig)
    definitions.random_puzzle.side_effect = ExternalServiceError("must not refetch")
    definitions.random_photo.side_effect = ExternalServiceError("must not refetch")
    restored = await current(rig, record)
    assert restored.value.model_dump() == original
    await click(rig, restored, "0", user_id=43)
    rig.backend.now = restored.value.deadline_at + timedelta(seconds=1)
    await settle(rig)
    closed = await current(rig, record)
    assert closed.value.phase == "closed" and closed.value.score_status == "recorded"
    assert closed.value.closed_at == restored.value.deadline_at
    assert len(await votes(rig, record)) == 2
    assert len([method for method in rig.session.methods if isinstance(method, SendPhoto)]) == 1


async def test_many_voters_are_paged_on_the_original_message_after_restart(rig):
    record = await start(rig)
    await asyncio.gather(*(click(rig, record, user_id % 6, user_id=user_id) for user_id in range(61)))
    await click(rig, record, "finish")
    await settle(rig)
    restart(rig)
    restored = await current(rig, record)
    all_votes = [vote.value for vote in await votes(rig, record)]
    rendered = definitions.DEFINITIONS[rig.feature].render(restored.value, all_votes)
    seen = set()
    for page in range(rendered.pages):
        await click(rig, restored, f"page_{page}")
        await settle(rig)
        method = edits(rig)[-1]
        caption = text_of(method)
        entities = method.media.caption_entities if isinstance(method, EditMessageMedia) else method.caption_entities
        assert len(caption.encode("utf-16-le")) // 2 <= 1024 and len(entities) < 100
        seen.update(entity.url for entity in entities if entity.url and entity.url.startswith("tg://user"))
        assert all(
            len(button.callback_data.encode()) <= 64
            for row in (method.reply_markup.inline_keyboard if method.reply_markup else [])
            for button in row
        )
    assert seen == {f"tg://user?id={user_id}" for user_id in range(61)}
    assert {method.message_id for method in edits(rig)} == {record.value.message_id}
    assert not any(isinstance(method, SendMessage) for method in rig.session.methods)
    assert (await current(rig, record)).value.score_status == "recorded"


async def test_completed_navigation_does_not_change_new_active_round(rig):
    previous = await start(rig)
    await click(rig, previous, "0")
    await click(rig, previous, "finish")
    await settle(rig)
    next_message = rig.message.model_copy(update={"message_id": 11})
    newer = await start(rig, next_message)
    restart(rig)
    await click(rig, previous, "page_0")
    await click(rig, previous, "0", user_id=77)
    await settle(rig)
    assert (await current(rig, newer)).value.phase == "active"
    assert not await votes(rig, newer) and len(await votes(rig, previous)) == 1
    assert edits(rig)[-1].message_id == previous.value.message_id


async def test_expired_result_buttons_are_rejected_and_cleanup_is_scoped(rig):
    record = await start(rig)
    await asyncio.gather(*(click(rig, record, i % 6, user_id=i) for i in range(61)))
    await click(rig, record, "finish")
    await settle(rig)
    other = await start(rig, make_message(rig.bot, message_id=11, chat={"id": -100999, "type": "supergroup"}, date=rig.backend.now))
    rig.backend.now += timedelta(days=1, seconds=1)
    restart(rig)
    await click(rig, record, "page_0")
    assert "недоступен" in rig.session.methods[-1].text
    await settle(rig)
    assert await current(rig, record) is None and not await votes(rig, record)
    assert await current(rig, other) is not None
    chat = await rig.quiz.collections[rig.feature].chats.get(record.scope, "state")
    assert chat.value.recent == [record.value.question.identity]


async def test_no_database_never_accepts_a_vote_or_starts_an_ephemeral_game(rig):
    record = await start(rig)
    rig.backend.fail = True
    await click(rig, record, "0")
    assert "Не удалось подтвердить" in rig.session.methods[-1].text
    await rig.quiz.start(rig.feature, rig.message.model_copy(update={"message_id": 11}))
    assert "Не удалось сохранить" in rig.session.methods[-1].text
    assert len([method for method in rig.session.methods if isinstance(method, SendPhoto)]) == 1
    rig.backend.fail = False
    assert not await votes(rig, record)


async def test_question_unknown_fields_survive_an_ordinary_vote(rig):
    record = await start(rig)
    for row in rig.backend.records.values():
        if row["collection"] == "rounds" and row["key"] == record.key:
            row["payload"]["future_outer"] = {"keep": 1}
            row["payload"]["question"]["future_question"] = [1, 2]
    await click(rig, record, "0")
    value = (await current(rig, record)).value.model_dump()
    assert value["future_outer"] == {"keep": 1} and value["question"]["future_question"] == [1, 2]


async def test_future_record_version_is_rejected_without_overwriting(rig):
    record = await start(rig)
    for row in rig.backend.records.values():
        if row["collection"] == "rounds" and row["key"] == record.key:
            row["payload_version"] = 2
    await click(rig, record, "0")
    assert "Не удалось подтвердить" in rig.session.methods[-1].text
    assert not await votes(rig, record)


async def test_lost_score_commit_reply_replays_the_same_transaction_once(rig, monkeypatch):
    from msu_hub_bot.storage.supabase import RepositoryFailure, RepositoryUnavailable

    record = await start(rig)
    await click(rig, record, record.value.question.answer)
    await click(rig, record, "finish")
    original = rig.backend.feature_request
    lost = False
    attempts = []

    async def uncertain(operation, request):
        nonlocal lost
        result = await original(operation, request)
        if operation == "commit" and any(put["collection"] == "scores" for put in request["puts"]):
            attempts.append(request)
            if not lost:
                lost = True
                raise RepositoryUnavailable(RepositoryFailure.UNAVAILABLE)
        return result

    monkeypatch.setattr(rig.backend, "feature_request", uncertain)
    await settle(rig)
    result = await current(rig, record)
    assert await score_values(rig, result.value.score_day) == {42: 1}
    assert result.value.score_status == "recorded" and result.value.score_count == 1
    assert len(attempts) == 2 and attempts[0] == attempts[1]


async def test_many_scores_use_guarded_batches_without_exceeding_transaction_limits(rig):
    record = await start(rig)
    answer = record.value.question.answer
    await asyncio.gather(*(click(rig, record, answer, user_id=uid) for uid in range(65)))
    await click(rig, record, "finish")
    await settle(rig)
    closed = await current(rig, record)
    assert closed.value.score_status == "recorded" and closed.value.score_count == 65
    assert await score_values(rig, closed.value.score_day) == dict.fromkeys(range(65), 1)
    batches = [
        request
        for operation, request in rig.backend.calls
        if operation == "commit" and any(put["collection"] == "scores" for put in request["puts"])
    ]
    assert len(batches) == 3
    assert [sum(put["collection"] == "scores" for put in batch["puts"]) for batch in batches] == [30, 30, 5]
    for batch in batches:
        assert len(batch["guards"]) <= 64
        assert len(batch["puts"]) + len(batch["deletes"]) + len(batch["jobs"]) + len(batch["cancel_jobs"]) <= 64
        assert {guard["collection"] for guard in batch["guards"]} == {"rounds", "votes", "scores"}
        assert next(put for put in batch["puts"] if put["collection"] == "rounds")["payload"]["score_cursor"]


async def test_restart_after_one_committed_batch_resumes_without_duplicate_points(rig, monkeypatch):
    record = await start(rig)
    await asyncio.gather(*(click(rig, record, record.value.question.answer, user_id=uid) for uid in range(65)))
    await click(rig, record, "finish")
    original = rig.backend.feature_request
    crashed = False

    async def crash_after_commit(operation, request):
        nonlocal crashed
        result = await original(operation, request)
        if not crashed and operation == "commit" and any(put["collection"] == "scores" for put in request["puts"]):
            crashed = True
            raise asyncio.CancelledError()
        return result

    monkeypatch.setattr(rig.backend, "feature_request", crash_after_commit)
    with pytest.raises(asyncio.CancelledError):
        await rig.worker.run_once()
    pending = await current(rig, record)
    assert pending.value.score_status == "pending" and pending.value.score_count == 30
    assert len(await score_values(rig, pending.value.score_day)) == 30
    assert pending.value.score_cursor is not None
    rig.backend.now += timedelta(seconds=61)
    restart(rig)
    await settle(rig)
    closed = await current(rig, record)
    assert closed.value.score_status == "recorded" and closed.value.score_count == 65
    assert await score_values(rig, closed.value.score_day) == dict.fromkeys(range(65), 1)


async def test_two_stale_workers_cannot_both_apply_the_same_score_batch(rig, monkeypatch):
    from msu_hub_bot.games.quiz import QuizService
    from msu_hub_bot.storage.features import Conflict, FeatureWorker, Job, JobContext

    record = await start(rig)
    await click(rig, record, record.value.question.answer)
    await click(rig, record, "finish")
    claimed = await rig.backend.feature_request(
        "claim_jobs", {"handlers": [{"feature": rig.feature, "kind": "settle"}], "limit": 1, "lease_seconds": 60}
    )
    context = JobContext(rig.store, Job.model_validate(claimed[0]))
    competitor = QuizService(rig.bot, rig.store, FeatureWorker(rig.store))
    competitor.clock = rig.quiz.clock
    original = rig.backend.feature_request
    ready = asyncio.Event()
    writers = 0

    async def race(operation, request):
        nonlocal writers
        if operation == "commit" and any(put["collection"] == "scores" for put in request["puts"]):
            writers += 1
            if writers == 2:
                ready.set()
            await ready.wait()
        return await original(operation, request)

    monkeypatch.setattr(rig.backend, "feature_request", race)
    results = await asyncio.gather(rig.quiz._settle(context), competitor._settle(context), return_exceptions=True)
    assert any(isinstance(result, Conflict) for result in results)
    assert writers == 2
    closed = await current(rig, record)
    assert closed.value.score_count == 1 and closed.value.score_status == "recorded"
    assert await score_values(rig, closed.value.score_day) == {42: 1}


async def test_pending_settlements_cannot_reorder_floored_scores(rig):
    first = await start(rig)
    await click(rig, first, (first.value.question.answer + 1) % 6)
    await click(rig, first, "finish")
    first_job = next(job for job in rig.backend.jobs.values() if job["kind"] == "settle")
    first_job["state"] = "held"
    second = await start(rig, rig.message.model_copy(update={"message_id": 11}))
    await click(rig, second, second.value.question.answer)
    await click(rig, second, "finish")
    await settle(rig)
    day = (await current(rig, first)).value.score_day
    assert await score_values(rig, day) == {}
    assert (await current(rig, second)).value.score_status == "pending"
    first_job["state"] = "pending"
    await settle(rig)
    assert await score_values(rig, day) == {42: 1}
    assert (await current(rig, first)).value.score_status == "recorded"
    assert (await current(rig, second)).value.score_status == "recorded"


async def test_prior_day_held_settlement_does_not_block_todays_independent_scores(rig):
    rig.backend.now = datetime(2030, 1, 1, 12, tzinfo=UTC)
    first = await start(rig)
    await click(rig, first, "0")
    await click(rig, first, "finish")
    for job in rig.backend.jobs.values():
        if job["kind"] == "settle":
            job["state"] = "held"
    rig.backend.now += timedelta(days=1)
    second = await start(rig, rig.message.model_copy(update={"message_id": 11}))
    await click(rig, second, second.value.question.answer)
    await click(rig, second, "finish")
    await settle(rig)
    assert (await current(rig, first)).value.score_status == "pending"
    closed = await current(rig, second)
    assert closed.value.score_status == "recorded"
    assert await score_values(rig, closed.value.score_day) == {42: 1}


async def test_held_settlement_and_votes_survive_until_reconciled_without_a_score_expiry(rig):
    record = await start(rig)
    await click(rig, record, record.value.question.answer)
    await click(rig, record, "finish")
    job = next(job for job in rig.backend.jobs.values() if job["kind"] == "settle")
    job["state"] = "held"
    closed = await current(rig, record)
    rig.backend.now += timedelta(days=90)
    restart(rig)
    await settle(rig)
    assert (await current(rig, record)).value.score_status == "pending"
    assert len(await votes(rig, record)) == 1
    assert await score_values(rig, closed.value.score_day) == {}
    assert not any(job["kind"] == "score_expiry" for job in rig.backend.jobs.values())
    job["state"] = "pending"
    await settle(rig)
    assert await score_values(rig, closed.value.score_day) == {42: 1}
    assert await current(rig, record) is None
    assert not await votes(rig, record)
    assert all(row.expires_at is None for row in await score_rows(rig, closed.value.score_day))


async def test_overdue_round_uses_saved_deadline_day_after_restart(rig):
    rig.backend.now = datetime(2030, 1, 1, 20, 45, tzinfo=UTC)
    record = await start(rig)
    await click(rig, record, record.value.question.answer)
    rig.backend.now = datetime(2030, 1, 1, 21, 1, tzinfo=UTC)
    restart(rig)
    await settle(rig)
    closed = await current(rig, record)
    assert closed.value.score_day.isoformat() == "2030-01-01"
    assert closed.value.closed_at == record.value.deadline_at


async def test_manual_finish_uses_its_own_saved_moscow_day(rig):
    rig.backend.now = datetime(2030, 1, 1, 20, 59, tzinfo=UTC)
    record = await start(rig)
    await click(rig, record, "0")
    rig.backend.now = datetime(2030, 1, 1, 21, 1, tzinfo=UTC)
    await click(rig, record, "finish")
    await settle(rig)
    assert (await current(rig, record)).value.score_day.isoformat() == "2030-01-02"


async def test_photo_failure_releases_slot_and_preserves_recent_history(rig):
    provider = definitions.random_puzzle if rig.feature == "chess" else definitions.random_photo
    provider.side_effect = ExternalServiceError("synthetic provider failure")
    record = await start(rig)
    assert record.value.phase == "abandoned"
    assert rig.session.methods[-1].text == "Ошибка, попробуйте еще раз"
    assert not any(isinstance(method, SendPhoto) for method in rig.session.methods)
    chat = await rig.quiz.collections[rig.feature].chats.get(record.scope, "state")
    assert chat.value.active is None and chat.value.recent == []


async def test_initial_send_uncertainty_is_recovered_by_its_own_bot_callback(rig, monkeypatch):
    request = rig.session.make_request

    async def lost(bot, method, timeout=None):
        result = await request(bot, method, timeout)
        if isinstance(method, SendPhoto):
            raise TimeoutError("Telegram accepted the photo but its reply was lost")
        return result

    monkeypatch.setattr(rig.session, "make_request", lost)
    record = await start(rig)
    assert record.value.phase == "publishing" and record.value.message_id is None
    delivered = next(message for message in rig.session.messages.values())
    restart(rig)
    await click(rig, record, "0", message=delivered)
    restored = await current(rig, record)
    assert restored.value.phase == "active" and restored.value.message_id == delivered.message_id
    assert restored.value.deadline_at == delivered.date + timedelta(minutes=10)
    assert len(await votes(rig, record)) == 1
    assert len([method for method in rig.session.methods if isinstance(method, SendPhoto)]) == 1


async def test_unknown_delivery_is_never_blindly_resent_and_eventually_releases_slot(rig):
    async def fail(_):
        raise TimeoutError("unknown photo delivery")

    rig.session.photo_hook = fail
    record = await start(rig)
    assert record.value.phase == "publishing"
    restart(rig)
    rig.backend.now += timedelta(minutes=2)
    await settle(rig)
    assert not any(isinstance(method, SendPhoto) for method in rig.session.methods)
    assert await current(rig, record) is None
    chat = await rig.quiz.collections[rig.feature].chats.get(record.scope, "state")
    assert chat.value.active is None and not chat.value.recent


async def test_untrusted_callback_cannot_bind_unknown_message(rig):
    async def fail(_):
        raise TimeoutError("unknown delivery")

    rig.session.photo_hook = fail
    record = await start(rig)
    await click(rig, record, "0", message=rig.message)
    assert (await current(rig, record)).value.message_id is None
    assert not await votes(rig, record)


@pytest.mark.parametrize("wrong", ["photo", "keyboard", "topic"])
async def test_other_bot_message_cannot_hijack_uncertain_publication(rig, monkeypatch, wrong):
    request = rig.session.make_request

    async def lost(bot, method, timeout=None):
        result = await request(bot, method, timeout)
        if isinstance(method, SendPhoto):
            raise TimeoutError("lost photo reply")
        return result

    monkeypatch.setattr(rig.session, "make_request", lost)
    record = await start(rig)
    delivered = next(iter(rig.session.messages.values()))
    change = {"photo": None} if wrong == "photo" else {"reply_markup": None} if wrong == "keyboard" else {"message_thread_id": 99}
    unrelated = delivered.model_copy(update=change)
    await click(rig, record, "0", message=unrelated)
    assert (await current(rig, record)).value.message_id is None
    assert not await votes(rig, record)


async def test_slow_provider_does_not_consume_the_published_round_deadline(rig):
    provider = definitions.random_puzzle if rig.feature == "chess" else definitions.random_photo
    original = provider.return_value

    async def slow(_):
        rig.backend.now += timedelta(seconds=7)
        return original

    provider.side_effect = slow
    record = await start(rig)
    assert record.value.published_at == rig.backend.now
    assert record.value.deadline_at - record.value.prepared_at == timedelta(minutes=10, seconds=7)


@pytest.mark.parametrize("stage", ["provider", "upload"])
async def test_total_photo_deadline_cancels_blocked_work_and_never_resends(rig, monkeypatch, stage):
    monkeypatch.setattr("msu_hub_bot.games.quiz.PHOTO_TIMEOUT", 0.02)
    cancelled = asyncio.Event()

    async def blocked(_):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    if stage == "provider":
        provider = definitions.random_puzzle if rig.feature == "chess" else definitions.random_photo
        provider.side_effect = blocked
    else:
        rig.session.photo_hook = blocked
    record = await start(rig)
    assert cancelled.is_set()
    assert record.value.phase == ("abandoned" if stage == "provider" else "publishing")
    assert not any(isinstance(method, SendPhoto) for method in rig.session.methods)


async def test_loading_reserves_only_its_chat_and_forum_topics_share_game_slot(rig):
    entered, release = asyncio.Event(), asyncio.Event()
    provider = definitions.random_puzzle if rig.feature == "chess" else definitions.random_photo
    value = provider.return_value

    async def slow(_):
        entered.set()
        await release.wait()
        return value

    provider.side_effect = slow
    loading = asyncio.create_task(start(rig))
    await entered.wait()
    await rig.quiz.start(rig.feature, rig.message.model_copy(update={"message_id": 11, "message_thread_id": 99}))
    assert "прошлое задание" in rig.session.methods[-1].text
    release.set()
    record = await loading
    another = await start(rig, make_message(rig.bot, chat={"id": -100222, "type": "supergroup"}, date=rig.backend.now))
    assert record.value.phase == "active" and another.value.phase == "active"


async def test_valid_out_of_range_pages_clamp_without_repeated_edits(rig):
    record = await start(rig)
    await asyncio.gather(*(click(rig, record, 0, user_id=i) for i in range(9)))
    await click(rig, record, "page_99999999")
    await settle(rig)
    assert "Страница 3/3" in text_of(edits(rig)[-1])
    count = len(edits(rig))
    await click(rig, record, "page_99999999")
    await settle(rig)
    assert len(edits(rig)) == count


async def test_participant_bound_is_checked_before_accepting_more_votes(rig):
    record = await start(rig)
    for row in rig.backend.records.values():
        if row["collection"] == "rounds" and row["key"] == record.key:
            row["payload"]["vote_count"] = 10_000
    await click(rig, record, "0")
    assert "слишком много ответов" in rig.session.methods[-1].text
    assert not await votes(rig, record)


async def test_cancelled_provider_start_keeps_recoverable_cleanup_without_photo(rig):
    entered = asyncio.Event()

    async def blocked(_):
        entered.set()
        await asyncio.Event().wait()

    provider = definitions.random_puzzle if rig.feature == "chess" else definitions.random_photo
    provider.side_effect = blocked
    task = asyncio.create_task(start(rig))
    await entered.wait()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await settle(rig)
    assert not any(isinstance(method, SendPhoto) for method in rig.session.methods)
    assert not [row for row in rig.backend.records.values() if row["collection"] == "rounds"]


async def test_deleted_photo_never_generates_replacement_messages_and_buttons_can_retry(rig):
    record = await start(rig)
    await click(rig, record, "0")
    rig.session.caption_error = rig.session.media_error = True
    await click(rig, record, "finish")
    await settle(rig)
    assert not any(isinstance(method, SendMessage) for method in rig.session.methods)
    rig.session.caption_error = rig.session.media_error = False
    restart(rig)
    await click(rig, record, "finish")
    await settle(rig)
    assert "Правильный ход" in text_of(edits(rig)[-1]) if rig.feature == "chess" else "На снимке" in text_of(edits(rig)[-1])
    assert len([method for method in rig.session.methods if isinstance(method, SendPhoto)]) == 1
    assert (await current(rig, record)).value.score_status == "recorded"


async def test_not_modified_accepts_the_view_without_repeated_edits(rig):
    record = await start(rig)

    async def not_modified(method):
        raise TelegramBadRequest(method=method, message="Bad Request: message is not modified: content and markup are identical")

    rig.session.edit_hook = not_modified
    await click(rig, record, "0")
    await settle(rig)
    count = len(edits(rig))
    await click(rig, record, "page_0")
    await settle(rig)
    assert len(edits(rig)) == count


async def test_chess_transient_media_failure_keeps_caption_and_automatically_repairs_photo(rig):
    if rig.feature != "chess":
        return
    # Exercise the fallback itself without settlement superseding its render lease.
    rig.worker.concurrency = 1
    record = await start(rig)
    await click(rig, record, "0")
    failed = False

    async def transient(method):
        nonlocal failed
        if isinstance(method, EditMessageMedia) and not failed:
            failed = True
            raise TimeoutError("lost media edit reply")

    rig.session.edit_hook = transient
    await click(rig, record, "finish")
    await settle(rig)
    assert any(isinstance(method, EditMessageCaption) and "Правильный ход" in method.caption for method in edits(rig))
    rig.backend.now += timedelta(seconds=3)
    await settle(rig)
    assert isinstance(edits(rig)[-1], EditMessageMedia)
    assert (await current(rig, record)).value.score_status == "recorded"


async def test_country_only_result_has_no_empty_city_and_preserves_credit(rig):
    if rig.feature != "geoguess":
        return
    definitions.random_photo.return_value = replace(PHOTO, city="")
    record = await start(rig)
    await click(rig, record, "finish")
    await settle(rig)
    assert "На снимке — 🇳🇴 Норвегия." in text_of(edits(rig)[-1])
    assert "OpenStreetMap" in text_of(edits(rig)[-1])


async def test_burst_of_votes_coalesces_into_the_latest_caption(rig):
    record = await start(rig)
    await asyncio.gather(*(click(rig, record, i % 6, user_id=i) for i in range(40)))
    await settle(rig)
    assert len(await votes(rig, record)) == 40
    assert len(edits(rig)) == 1 and "Ответили: 40" in text_of(edits(rig)[0])


async def test_leaderboard_uses_one_moscow_day_and_native_entities(rig):
    handler = chess.Chess if rig.feature == "chess" else geoguess.Geoguess
    rig.backend.now = datetime(2030, 1, 1, 12, tzinfo=UTC)
    record = await start(rig)
    await click(rig, record, record.value.question.answer, name="User <name>", username="user_name")
    await click(rig, record, "finish")
    await settle(rig)
    restart(rig)
    await handler.top(rig.message, rig.quiz)
    result = rig.session.methods[-1]
    assert "01.01.2030" in result.text and "User <name>" in result.text and "@user_name" in result.text
    assert result.parse_mode is None and any(entity.url == "tg://user?id=42" for entity in result.entities)
    rig.backend.fail = True
    await handler.top(rig.message, rig.quiz)
    assert rig.session.methods[-1].text == "Рейтинг сейчас недоступен."


async def test_scores_floor_at_zero_and_refresh_labels_between_rounds(rig):
    for index, (correct, name, username) in enumerate(
        [(False, "First <name>", "first_name"), (True, "Renamed & player", None), (False, "Final name", "final_name")]
    ):
        record = await start(rig, rig.message.model_copy(update={"message_id": index + 10}))
        choice = record.value.question.answer if correct else (record.value.question.answer + 1) % 6
        await click(rig, record, choice, name=name, username=username)
        await click(rig, record, "finish")
        await settle(rig)
        closed = await current(rig, record)
        (player,) = await score_rows(rig, closed.value.score_day)
        assert player.value.points == (1 if correct else 0)
        assert player.value.name == name and player.value.username == username
        assert player.expires_at is None
        restart(rig)


@pytest.mark.parametrize("invalid", ["missing_points", "negative_points", "future_version", "wrong_user", "wrong_day"])
async def test_unreadable_score_is_held_without_replacing_it_with_defaults(rig, invalid):
    from copy import deepcopy

    first = await start(rig)
    await click(rig, first, first.value.question.answer)
    await click(rig, first, "finish")
    await settle(rig)
    identity, score = next((key, row) for key, row in rig.backend.records.items() if row["collection"] == "scores")
    if invalid == "missing_points":
        del score["payload"]["points"]
    elif invalid == "negative_points":
        score["payload"]["points"] = -1
    elif invalid == "future_version":
        score["payload_version"] = 2
    elif invalid == "wrong_user":
        score["payload"]["user_id"] = 99
    else:
        score["parent"] = "2000-01-01"
    before = deepcopy(score)
    second = await start(rig, rig.message.model_copy(update={"message_id": 11}))
    await click(rig, second, second.value.question.answer)
    await click(rig, second, "finish")
    await settle(rig)
    state = (await current(rig, second)).value
    assert state.score_status == "pending" and state.score_count == 0
    assert rig.backend.records[identity] == before
    assert next(job for job in rig.backend.jobs.values() if job["key"] == f"settle:{second.key}")["state"] == "held"


async def test_score_updates_preserve_unknown_metadata(rig):
    first = await start(rig)
    await click(rig, first, first.value.question.answer)
    await click(rig, first, "finish")
    await settle(rig)
    score = next(row for row in rig.backend.records.values() if row["collection"] == "scores")
    score["payload"]["future_metadata"] = {"keep": [1, 2]}
    second = await start(rig, rig.message.model_copy(update={"message_id": 11}))
    await click(rig, second, second.value.question.answer)
    await click(rig, second, "finish")
    await settle(rig)
    day = (await current(rig, second)).value.score_day
    (result,) = await score_rows(rig, day)
    assert result.value.points == 2 and result.value.model_dump()["future_metadata"] == {"keep": [1, 2]}


async def test_round_without_players_creates_no_score_documents(rig):
    record = await start(rig)
    await click(rig, record, "finish")
    await settle(rig)
    closed = await current(rig, record)
    assert closed.value.score_status == "recorded" and closed.value.score_count == 0
    assert await score_values(rig, closed.value.score_day) == {}


async def test_scores_are_isolated_by_game_chat_and_moscow_day(rig):
    rig.backend.now = datetime(2030, 1, 1, 20, 45, tzinfo=UTC)
    first = await start(rig)
    await click(rig, first, first.value.question.answer)
    await click(rig, first, "finish")
    await settle(rig)
    day = (await current(rig, first)).value.score_day

    other_feature = "geoguess" if rig.feature == "chess" else "chess"
    assert await score_values(rig, day, feature=other_feature) == {}
    other_message = make_message(rig.bot, chat={"id": -100222, "type": "supergroup"}, date=rig.backend.now)
    second = await start(rig, other_message)
    await click(rig, second, (second.value.question.answer + 1) % 6)
    await click(rig, second, "finish")
    await settle(rig)
    assert await score_values(rig, day, chat_id=-100222) == {42: 0}
    assert await score_values(rig, day) == {42: 1}

    rig.backend.now = datetime(2030, 1, 1, 21, 1, tzinfo=UTC)
    next_day = await start(rig, rig.message.model_copy(update={"message_id": 11}))
    await click(rig, next_day, (next_day.value.question.answer + 1) % 6)
    await click(rig, next_day, "finish")
    await settle(rig)
    tomorrow = (await current(rig, next_day)).value.score_day
    assert tomorrow == day + timedelta(days=1)
    assert await score_values(rig, tomorrow) == {42: 0}
    assert await score_values(rig, day) == {42: 1}


async def test_leaderboard_reads_every_page_and_captures_the_moscow_day_once(rig, monkeypatch):
    from msu_hub_bot.games.models import Score

    rig.backend.now = datetime(2030, 1, 1, 20, 59, tzinfo=UTC)
    day = rig.backend.now.date()
    scope = rig.quiz._scope(rig.message.chat.id)
    collection = rig.quiz.collections[rig.feature].scores
    for offset in range(0, 205, 30):
        tx = rig.store.transaction(rig.feature, scope, operation_id=f"seed-{offset}")
        for uid in range(1000 + offset, 1000 + min(205, offset + 30)):
            key = f"{day.isoformat()}:{uid}"
            tx.expect_absent("scores", key)
            tx.put(collection, key, Score(user_id=uid, name=f"Player {uid}", username=None, points=uid), parent=day.isoformat())
        await tx.commit()
    original = rig.backend.feature_request
    pages = []

    async def midnight(operation, request):
        if operation == "list" and request["collection"] == "scores":
            pages.append(request)
            rig.backend.now = datetime(2030, 1, 1, 21, 1, tzinfo=UTC)
        return await original(operation, request)

    monkeypatch.setattr(rig.backend, "feature_request", midnight)
    body = await rig.quiz.ranking(rig.feature, rig.message.chat.id)
    text, entities = body.render()
    assert "01.01.2030" in text
    assert [entity.url for entity in entities if entity.url and entity.url.startswith("tg://user")] == [
        f"tg://user?id={uid}" for uid in range(1204, 1194, -1)
    ]
    assert len(pages) == 2 and {page["parent"] for page in pages} == {day.isoformat()}


async def test_recent_history_survives_restart_and_keeps_last_fifteen_delivered_questions(rig):
    identities = []
    for index in range(17):
        if rig.feature == "chess":
            definitions.random_puzzle.return_value = replace(PUZZLE, id=f"P{index:04}")
        else:
            definitions.random_photo.return_value = replace(PHOTO, country=f"Country {index}")
        record = await start(rig, rig.message.model_copy(update={"message_id": index + 10}))
        identities.append(record.value.question.identity)
        await click(rig, record, "finish")
        await settle(rig)
        restart(rig)
    chat = await rig.quiz.collections[rig.feature].chats.get(record.scope, "state")
    assert chat.value.recent == identities[-15:]
    provider = definitions.random_puzzle if rig.feature == "chess" else definitions.random_photo
    assert provider.call_args.args[0] == tuple(identities[-16:-1])


async def test_store_serialization_contains_no_telegram_objects_tasks_or_rendered_media(rig):
    record = await start(rig)
    await click(rig, record, "0")
    import json

    encoded = json.dumps(list(rig.backend.records.values()))
    assert "asyncio" not in encoded and "caption_entities" not in encoded and "synthetic-board" not in encoded
    assert "message_id" in encoded and "question" in encoded and "accepted_at" in encoded


def test_geography_country_and_user_labels_are_safe():
    from msu_hub_bot.commands.geoguess_view import country_label
    from msu_hub_bot.commands.quiz_view import user_label

    assert country_label("Норвегия") == "🇳🇴 Норвегия"
    assert country_label("Unlisted <place>") == "Unlisted <place>"
    assert user_label(42, "User <name>", None).as_html() == '<a href="tg://user?id=42">User &lt;name&gt;</a>'
