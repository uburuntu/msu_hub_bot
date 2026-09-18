"""Quiz behavior across concurrent inputs, uncertain delivery and process restarts."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import AnswerCallbackQuery, EditMessageCaption, EditMessageMedia, SendMessage, SendPhoto

from msu_hub_bot.commands import chess, geoguess
from msu_hub_bot.games import definitions, scores
from msu_hub_bot.providers.exceptions import ExternalServiceError
from quiz_helpers import PHOTO, PNG, PUZZLE, click, edits, restart, rig as rig, settle, start, text_of
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
    key = scores.score_key(rig.feature, rig.message.chat.id, stored.value.score_day)
    assert rig.client.scores[key] == {"42": 1, "43": 0}
    assert rig.client.hashes[key + ":names"]["42"] == "User 42 <&>"
    rig.client.eval.assert_awaited_once()
    result = text_of(edits(rig)[-1])
    assert "Угадали 1 из 2" in result and "+1" in result and "−1" in result
    if rig.feature == "chess":
        assert "Rxc8+" in result and isinstance(edits(rig)[-1], EditMessageMedia)
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
    rig.client.eval.assert_awaited_once()


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
    rig.client.eval.assert_awaited_once()


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


async def test_lost_redis_response_retries_frozen_settlement_once(rig):
    record = await start(rig)
    await click(rig, record, record.value.question.answer)
    original = rig.client.apply
    calls = 0

    async def uncertain(*args):
        nonlocal calls
        calls += 1
        result = await original(*args)
        if calls == 1:
            raise TimeoutError("reply lost after committing scores")
        return result

    rig.client.eval.side_effect = uncertain
    await click(rig, record, "finish")
    await settle(rig)
    pending = await current(rig, record)
    assert pending.value.score_status == "pending"
    rig.backend.now += timedelta(seconds=3)
    restart(rig)
    await settle(rig)
    result = await current(rig, record)
    key = scores.score_key(rig.feature, result.value.chat_id, result.value.score_day)
    assert rig.client.scores[key] == {"42": 1}
    assert result.value.score_status == "recorded" and calls == 2
    assert rig.client.eval.call_args_list[0].args == rig.client.eval.call_args_list[1].args


async def test_pending_settlements_cannot_reorder_floored_scores(rig):
    first = await start(rig)
    await click(rig, first, (first.value.question.answer + 1) % 6)
    rig.client.eval.side_effect = TimeoutError("temporary Redis outage")
    await click(rig, first, "finish")
    await settle(rig)
    second = await start(rig, rig.message.model_copy(update={"message_id": 11}))
    await click(rig, second, second.value.question.answer)
    await click(rig, second, "finish")
    await settle(rig)
    assert rig.client.eval.await_count == 1
    rig.client.eval.side_effect = rig.client.apply
    rig.backend.now += timedelta(seconds=3)
    await settle(rig)
    key = scores.score_key(rig.feature, first.value.chat_id, (await current(rig, first)).value.score_day)
    assert rig.client.scores[key] == {"42": 1}
    assert [call.args[7] for call in rig.client.eval.call_args_list] == [first.key, first.key, second.key]


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
    assert rig.client.scores[scores.score_key(rig.feature, second.value.chat_id, closed.value.score_day)] == {"42": 1}


async def test_held_settlement_expires_and_releases_result_cleanup(rig):
    record = await start(rig)
    await click(rig, record, "0")
    await click(rig, record, "finish")
    for job in rig.backend.jobs.values():
        if job["kind"] == "settle":
            job["state"] = "held"
    closed = await current(rig, record)
    rig.backend.now = scores.score_expiry(closed.value.score_day) + timedelta(seconds=1)
    restart(rig)
    await settle(rig)
    rig.backend.now += timedelta(seconds=61)
    await settle(rig)
    assert await current(rig, record) is None and not await votes(rig, record)
    rig.client.eval.assert_not_awaited()
    assert all(job["state"] in {"cancelled", "complete", "expired"} for job in rig.backend.jobs.values())


async def test_redis_clock_expiry_is_not_reported_as_success(rig):
    record = await start(rig)
    await click(rig, record, "0")
    rig.client.eval.side_effect = None
    rig.client.eval.return_value = -1
    await click(rig, record, "finish")
    await settle(rig)
    assert (await current(rig, record)).value.score_status == "expired"
    assert "Не удалось подтвердить запись" in text_of(edits(rig)[-1])


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
    rig.client.eval.assert_awaited_once()


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
    rig.client.eval.assert_awaited_once()


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


async def test_leaderboard_uses_one_moscow_day_and_native_entities(rig, monkeypatch):
    module = chess if rig.feature == "chess" else geoguess
    handler = module.Chess if rig.feature == "chess" else module.Geoguess
    day = datetime(2030, 1, 1).date()
    monkeypatch.setattr(module, "today", lambda: day)
    rig.client.zrevrange.side_effect = None
    rig.client.zrevrange.return_value = [(b"42", 3)]
    rig.client.hget.side_effect = [b"User <name>", b"user_name"]
    await handler.top(rig.message, rig.redis)
    result = rig.session.methods[-1]
    assert "01.01.2030" in result.text and "User <name>" in result.text and "@user_name" in result.text
    assert result.parse_mode is None and any(entity.url == "tg://user?id=42" for entity in result.entities)
    rig.client.zrevrange.assert_awaited_once_with(scores.score_key(rig.feature, rig.message.chat.id, day), 0, 9, withscores=True)
    rig.client.zrevrange.side_effect = RuntimeError("score store unavailable")
    await handler.top(rig.message, rig.redis)
    assert rig.session.methods[-1].text == "Рейтинг сейчас недоступен."


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


def test_moscow_date_helper_uses_moscow_clock(monkeypatch):
    original = scores.today

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 16, 21, 1, tzinfo=UTC).astimezone(tz)

    monkeypatch.setattr(scores, "datetime", Clock)
    assert original().isoformat() == "2026-09-17"


def test_geography_country_and_user_labels_are_safe():
    from msu_hub_bot.commands.geoguess_view import country_label
    from msu_hub_bot.commands.quiz_view import user_label

    assert country_label("Норвегия") == "🇳🇴 Норвегия"
    assert country_label("Unlisted <place>") == "Unlisted <place>"
    assert user_label(42, "User <name>", None).as_html() == '<a href="tg://user?id=42">User &lt;name&gt;</a>'
