"""Raffle uniqueness and fixed winners through authenticated PostgreSQL RPCs."""

import asyncio
from copy import deepcopy

from test_feature_postgres import call

from msu_hub_bot.games.raffle import Person, RaffleStore
from msu_hub_bot.storage.features import FeatureStore
from msu_hub_bot.storage.supabase import RepositoryFailure, RepositoryUnavailable


class PostgreSQLRaffles:
    def __init__(self, db):
        self.db = db
        self.barrier = None
        self.contenders = set()
        self.stage = "members"
        self.lose_draw = False
        self.draw_requests = []

    async def feature_request(self, operation, request):
        is_join = operation == "commit" and any(put["collection"] == "members" for put in request["puts"])
        is_draw = operation == "commit" and any(
            put["collection"] == "rounds" and put["payload"]["status"] == "finished" for put in request["puts"]
        )
        contested = is_join if self.stage == "members" else is_draw
        if contested and self.barrier and len(self.contenders) < 2 and request["operation_id"] not in self.contenders:
            self.contenders.add(request["operation_id"])
            async with asyncio.timeout(10):
                await self.barrier.wait()
        result = await asyncio.to_thread(call, self.db, operation, request)
        if is_draw:
            self.draw_requests.append((deepcopy(request), result["outcome"]))
            if self.lose_draw and result["outcome"] == "committed":
                self.lose_draw = False
                raise RepositoryUnavailable(RepositoryFailure.UNAVAILABLE)
        return result


async def test_real_postgres_reconciles_duplicate_joins_and_contested_lost_draw(application_db):
    backend = PostgreSQLRaffles(application_db)
    first, second = (RaffleStore(999, FeatureStore(backend)) for _ in range(2))
    row, created = await first.create(-101, 7, 1, 1, Person(user_id=42, name="Организатор"))
    assert created
    row = await first.bind(row.scope, row.key, 101)
    backend.barrier = asyncio.Barrier(2)
    joined = await asyncio.gather(
        first.join(row.scope, row.key, Person(user_id=99, name="Первый")),
        second.join(row.scope, row.key, Person(user_id=99, name="Первый")),
    )
    assert sorted(is_new for _, is_new in joined) == [False, True]
    await first.join(row.scope, row.key, Person(user_id=100, name="Второй"))
    backend.contenders.clear()
    backend.stage = "draw"
    backend.lose_draw = True
    winners = await asyncio.gather(first.draw(row.scope, row.key, 42), second.draw(row.scope, row.key, 42))
    assert winners[0].value.winner == winners[1].value.winner
    assert winners[0].value.participants == 2
    assert {outcome for _, outcome in backend.draw_requests} == {"committed", "conflict", "replayed"}
    committed = next(request for request, outcome in backend.draw_requests if outcome == "committed")
    replayed = next(request for request, outcome in backend.draw_requests if outcome == "replayed")
    assert committed == replayed
    restarted = RaffleStore(999, FeatureStore(backend))
    final = await restarted.draw(row.scope, row.key, 42)
    assert final.value.winner == winners[0].value.winner
    entries, page, pages = await restarted.page(final, 0)
    assert {entry.user_id for entry in entries} == {99, 100}
    assert (page, pages) == (0, 1)
    assert final.expires_at == row.expires_at
