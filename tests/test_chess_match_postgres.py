"""Chess service recovery and contested Elo commits through authenticated SQL RPCs."""

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from aiogram import Bot
from aiogram.methods import EditMessageMedia, SendPhoto
from quiz_helpers import PNG
from test_chess_match_service import MatchSession, act, fresh, open_match, restart
from test_feature_postgres import call
from test_postgres_storage import literal

from msu_hub_bot.games.chess_play import service as service_module
from msu_hub_bot.games.chess_play.service import FEATURE, SCOPE
from msu_hub_bot.storage.features import RecordKey
from msu_hub_bot.storage.supabase import RepositoryFailure, RepositoryUnavailable


class PostgreSQLFeatures:
    def __init__(self, db):
        self.db = db
        self.now = datetime.now(UTC)
        self.settlement_commits = []
        self.race = None
        self.contenders = set()
        self.lose_settlement_response = False
        self.lost_operation = None

    async def feature_request(self, operation, request):
        settlement = (
            operation == "commit"
            and any(put["collection"] == "ratings" for put in request["puts"])
            and any(put["collection"] == "matches" and put["payload"]["rating_status"] == "settled" for put in request["puts"])
        )
        if settlement and self.race is not None and request["operation_id"] not in self.contenders and len(self.contenders) < 2:
            # Both jobs have read the same global rating before either commits.
            self.contenders.add(request["operation_id"])
            async with asyncio.timeout(10):
                await self.race.wait()
        result = await asyncio.to_thread(call, self.db, operation, request)
        if settlement:
            self.settlement_commits.append((deepcopy(request), result["outcome"]))
            if self.lose_settlement_response and result["outcome"] == "committed":
                self.lose_settlement_response = False
                self.lost_operation = request["operation_id"]
                raise RepositoryUnavailable(RepositoryFailure.UNAVAILABLE)
        return result


@pytest.fixture
async def pg_rig(application_db, monkeypatch):
    monkeypatch.setattr(service_module, "render_match", lambda game: PNG)
    backend = PostgreSQLFeatures(application_db)
    bot = Bot("999:" + "a" * 35, session=MatchSession())
    rig = SimpleNamespace(backend=backend, bot=bot)
    restart(rig)
    try:
        yield rig
    finally:
        rig.worker.stop()
        await bot.session.close()


def make_due(db, kind, row=None):
    """Advance only synthetic jobs, independently of the service's wall clock."""
    assert kind in {"deadline", "settle", "render"}
    target = "" if row is None else f" AND record_key=({literal(row.key)} #>> '{{}}')"
    db.run(f"""UPDATE msu_hub_private.feature_jobs SET run_at=clock_timestamp()-interval '1 second'
        WHERE feature='chess_play' AND kind='{kind}' AND state='pending'{target};""")


def postpone_renders(db):
    db.run("""UPDATE msu_hub_private.feature_jobs SET run_at=clock_timestamp()+interval '1 hour'
        WHERE feature='chess_play' AND kind='render' AND state='pending';""")


async def ratings(service):
    rows = await service.ratings.list(SCOPE)
    assert all(row.expires_at is None for row in rows)
    return {row.value.user_id: row.value.rating for row in rows}


async def test_concurrent_games_add_global_elo_once_after_lost_committed_response(pg_rig, application_db):
    rig = pg_rig
    first = await open_match(rig.service, chat_id=-201)
    second = await open_match(rig.service, chat_id=-202)
    await act(rig.service, first, user_id=51, action="join")
    await act(rig.service, second, user_id=52, action="join")
    first, second = await asyncio.gather(fresh(rig.service, first), fresh(rig.service, second))
    assert first.value.game.white.user_id == second.value.game.white.user_id == 42
    assert first.value.game.white_rating == second.value.game.white_rating == 800
    await asyncio.gather(
        act(rig.service, first, user_id=51, action="resign"),
        act(rig.service, second, user_id=52, action="resign"),
    )
    postpone_renders(application_db)
    rig.backend.race = asyncio.Barrier(2)
    rig.backend.lose_settlement_response = True
    make_due(application_db, "settle")
    assert await rig.worker.run_once() == 2
    assert {outcome for _, outcome in rig.backend.settlement_commits} == {"committed", "replayed", "conflict"}
    # Retry the losing CAS after its peer's change, retaining its own +16 delta.
    make_due(application_db, "settle")
    postpone_renders(application_db)
    assert await rig.worker.run_once() == 1
    assert await ratings(rig.service) == {42: 832, 51: 784, 52: 784}
    first, second = await asyncio.gather(fresh(rig.service, first), fresh(rig.service, second))
    assert first.value.rating_status == second.value.rating_status == "settled"
    assert sorted((first.value.ratings[0], second.value.ratings[0])) == [(800, 816), (816, 832)]
    assert first.value.ratings[1] == second.value.ratings[1] == (800, 784)
    assert first.expires_at is second.expires_at is None
    outcomes = [outcome for _, outcome in rig.backend.settlement_commits]
    assert outcomes.count("committed") == 2 and outcomes.count("conflict") == 1 and outcomes.count("replayed") == 1
    lost = [
        (request, outcome) for request, outcome in rig.backend.settlement_commits if request["operation_id"] == rig.backend.lost_operation
    ]
    assert [outcome for _, outcome in lost] == ["committed", "replayed"]
    assert lost[0][0] == lost[1][0]
    chats = await asyncio.gather(*(rig.service.chats.get(SCOPE, str(row.value.game.chat_id)) for row in (first, second)))
    assert all(chat.value.active is None for chat in chats)

    # A new process may claim settlement again; the committed marker wins.
    rig.worker.stop()
    restart(rig)
    for row in (first, second):
        tx = rig.store.transaction(FEATURE, SCOPE, operation_id=uuid4().hex)
        tx.expect(row)
        tx.schedule("settle:" + row.key, "settle", record=RecordKey("matches", row.key), run_at=datetime.now(UTC) - timedelta(seconds=1))
        await tx.commit()
    postpone_renders(application_db)
    assert await rig.worker.run_once() == 2
    assert await ratings(rig.service) == {42: 832, 51: 784, 52: 784}
    assert len(rig.backend.settlement_commits) == 4
    assert application_db.value("SELECT count(*) FROM msu_hub_private.feature_jobs WHERE feature='chess_play' AND state='held';") == 0


async def test_restart_recovers_selected_piece_absolute_deadline_and_same_board(pg_rig, application_db):
    rig = pg_rig
    row = await open_match(rig.service, chat_id=-301, thread_id=77)
    await act(rig.service, row, user_id=51, action="join")
    row = await fresh(rig.service, row)
    rig.backend.now += timedelta(seconds=12)
    await act(rig.service, row, user_id=42, action="move", value="e2e4")
    row = await fresh(rig.service, row)
    rig.backend.now += timedelta(seconds=8)
    await act(rig.service, row, user_id=51, action="pick", value="e7")
    row = await fresh(rig.service, row)
    original = row.value.game.model_dump()
    deadline = datetime.fromtimestamp(row.value.game.deadline(), UTC)
    rig.worker.stop()
    restart(rig)
    restored = await fresh(rig.service, row)
    assert restored.value.game.model_dump() == original
    assert restored.value.game.selected == "e7"
    assert restored.value.game.moves == ["e2e4"]
    assert restored.value.game.remaining(rig.backend.now.timestamp()) == (593, 592)
    assert restored.expires_at is None

    rig.backend.now = deadline + timedelta(seconds=1)
    postpone_renders(application_db)
    make_due(application_db, "deadline", restored)
    assert await rig.worker.run_once() == 1
    ended = await fresh(rig.service, row)
    assert ended.value.game.result == "timeout" and ended.value.game.winner == 42
    assert ended.value.game.remaining(rig.backend.now.timestamp()) == (593, 0)
    assert ended.value.finished_at == deadline
    assert ended.value.rating_status == "pending"
    assert (await rig.service.chats.get(SCOPE, "-301")).value.active is None
    make_due(application_db, "settle", ended)
    assert await rig.worker.run_once() == 1
    final = await fresh(rig.service, row)
    assert final.value.rating_status == "settled"
    assert final.value.ratings == ((800, 816), (800, 784))
    assert await ratings(rig.service) == {42: 816, 51: 784}
    make_due(application_db, "render", final)
    assert await rig.worker.run_once() == 1
    shown = await fresh(rig.service, final)
    assert shown.value.revealed
    assert shown.value.game.message_id == original["message_id"] and shown.value.game.thread_id == 77
    edits = [method for method in rig.bot.session.methods if isinstance(method, EditMessageMedia)]
    assert len(edits) == 1 and edits[0].message_id == original["message_id"]
    assert "Время вышло" in edits[0].media.caption and "Elo 800 → 816 (+16)" in edits[0].media.caption
    assert sum(isinstance(method, SendPhoto) for method in rig.bot.session.methods) == 1
