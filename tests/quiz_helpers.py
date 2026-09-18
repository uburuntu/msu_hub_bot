"""Network-free quiz fixtures; PostgreSQL contracts validate the backing protocol."""

import asyncio
from collections import defaultdict
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageCaption, EditMessageMedia, SendMessage, SendPhoto
from aiogram.types import CallbackQuery

from msu_hub_bot.games import definitions
from msu_hub_bot.games.quiz import QuizService
from msu_hub_bot.providers.chess import MoveOption, Puzzle
from msu_hub_bot.providers.geoguess import Photo
from msu_hub_bot.storage.features import FeatureStore, FeatureWorker
from msu_hub_bot.storage.supabase import RepositoryFailure, RepositoryUnavailable
from msu_hub_bot.telegram.wrapper import BotWrapper
from telegram_helpers import RecordingSession, make_message

PNG = b"\x89PNG\r\n\x1a\nsynthetic-board"
PUZZLE = Puzzle(
    "X0FOH",
    "rkb2R2/p1p4p/1pB1p3/2n1q3/8/P1p5/1PP3PP/1K3R2 w - - 0 1",
    ("f8c8", "b8c8", "f1f8"),
    tuple(
        MoveOption(move, label)
        for move, label in [
            ("f8f7", "Ладья f8 → f7"),
            ("f1f7", "Ладья f1 → f7"),
            ("c6d5", "Слон c6 → d5"),
            ("f8c8", "Ладья f8 → c8"),
            ("c6b5", "Слон c6 → b5"),
            ("f1e1", "Ладья f1 → e1"),
        ]
    ),
    ("Rxc8+", "Kxc8", "Rf8#"),
)
PHOTO = Photo(
    "Норвегия",
    "Берген",
    "https://upload.wikimedia.org/test.jpg",
    "https://commons.wikimedia.org/?curid=1",
    "Author <name>",
    "CC BY 3.0",
    "https://creativecommons.org/licenses/by/3.0",
)


class FeatureFixture:
    """Small transactional boundary double, including deliberate lost responses."""

    def __init__(self):
        self.now = datetime.now(UTC)
        self.records, self.jobs, self.receipts = {}, {}, {}
        self.calls = []
        self.fail = False
        self.lose_after_commit = 0

    def prefix(self, request):
        return request["feature"], request["scope"]["owner"], request["scope"]["key"]

    async def feature_request(self, operation, request):
        await asyncio.sleep(0)
        self.calls.append((operation, deepcopy(request)))
        if self.fail:
            raise RepositoryUnavailable(RepositoryFailure.UNAVAILABLE)
        if operation in {"get", "list"}:
            prefix = (*self.prefix(request), request["collection"])
            values = [
                row
                for identity, row in self.records.items()
                if identity[:4] == prefix and (row["expires_at"] is None or datetime.fromisoformat(row["expires_at"]) > self.now)
            ]
            if operation == "get":
                return deepcopy(next((row for row in values if row["key"] == request["key"]), None))
            return deepcopy(
                sorted(
                    [
                        row
                        for row in values
                        if (request["parent"] is None or row["parent"] == request["parent"])
                        and (request["status"] is None or row["status"] == request["status"])
                        and (request["after"] is None or row["key"] > request["after"])
                    ],
                    key=lambda row: row["key"],
                )[: request["limit"]]
            )
        if operation == "commit":
            prefix = self.prefix(request)
            receipt_key = *prefix, request["operation_id"]
            if receipt_key in self.receipts:
                old, result = self.receipts[receipt_key]
                return {**deepcopy(result), "outcome": "replayed" if old == request else "operation_mismatch"}
            for guard in request["guards"]:
                row = self.records.get((*prefix, guard["collection"], guard["key"]))
                if row is not None and row["expires_at"] is not None and datetime.fromisoformat(row["expires_at"]) <= self.now:
                    row = None
                if (None if row is None else row["etag"]) != guard["etag"]:
                    return {"outcome": "conflict", "records": []}
            changed = []
            for put in request["puts"]:
                identity = *prefix, put["collection"], put["key"]
                old = self.records.get(identity)
                row = {
                    **deepcopy(put),
                    "feature": request["feature"],
                    "scope": deepcopy(request["scope"]),
                    "etag": str(uuid4()),
                    "created_at": self.now.isoformat() if old is None else old["created_at"],
                    "updated_at": self.now.isoformat(),
                }
                self.records[identity] = row
                changed.append(deepcopy(row))
            for deleted in request["deletes"]:
                self.records.pop((*prefix, deleted["collection"], deleted["key"]), None)
            for scheduled in request["jobs"]:
                identity = *prefix, scheduled["key"]
                old = self.jobs.get(identity)
                self.jobs[identity] = {
                    **deepcopy(scheduled),
                    "feature": request["feature"],
                    "scope": deepcopy(request["scope"]),
                    "generation": 1 if old is None else old["generation"] + 1,
                    "sequence": len(self.jobs) if old is None else old["sequence"],
                    "attempts": 0,
                    "state": "pending",
                    "lease_token": None if old is None else old["lease_token"],
                    "lease_until": self.now if old is None else old["lease_until"],
                }
            for key in request["cancel_jobs"]:
                if job := self.jobs.get((*prefix, key)):
                    job["state"], job["generation"] = "cancelled", job["generation"] + 1
            result = {"outcome": "committed", "records": changed}
            self.receipts[receipt_key] = deepcopy(request), deepcopy(result)
            if self.lose_after_commit:
                self.lose_after_commit -= 1
                raise RepositoryUnavailable(RepositoryFailure.UNAVAILABLE)
            return result
        if operation == "claim_jobs":
            handlers = {(item["feature"], item["kind"]) for item in request["handlers"]}
            candidates = sorted(self.jobs.values(), key=lambda job: (job["run_at"], job["sequence"]))
            result = []
            for job in candidates:
                if (job["feature"], job["kind"]) not in handlers or job["state"] not in {"pending", "running"}:
                    continue
                if datetime.fromisoformat(job["run_at"]) > self.now or job["lease_until"] > self.now:
                    continue
                if job["serial_key"] is not None and any(
                    other["feature"] == job["feature"]
                    and other["scope"] == job["scope"]
                    and other["serial_key"] == job["serial_key"]
                    and other["sequence"] < job["sequence"]
                    and other["state"] in {"pending", "running", "held"}
                    for other in candidates
                ):
                    continue
                job["state"], job["lease_token"] = "running", str(uuid4())
                job["lease_until"] = self.now + timedelta(seconds=request["lease_seconds"])
                job["attempts"] += 1
                result.append(
                    {
                        key: deepcopy(job[key])
                        for key in (
                            "feature",
                            "scope",
                            "key",
                            "kind",
                            "record",
                            "generation",
                            "lease_token",
                            "run_at",
                            "attempts",
                            "retry_until",
                        )
                    }
                )
                if len(result) == request["limit"]:
                    break
            return result
        assert operation == "job_status"
        job = self.jobs.get((*self.prefix(request), request["key"]))
        same_lease = job is not None and job["lease_token"] == request["lease_token"]
        current = same_lease and job["generation"] == request["generation"] and job["lease_until"] > self.now
        action = request["action"]
        if same_lease and action == "complete" and not current:
            job["lease_until"] = self.now
        if current:
            if action == "renew":
                job["lease_until"] = self.now + timedelta(seconds=request["lease_seconds"])
            elif action != "check":
                job["lease_until"] = self.now
                job["state"] = {"complete": "complete", "hold": "held", "expire": "expired", "retry": "pending"}[action]
                if action == "retry":
                    job["run_at"] = request["run_at"]
        return {"current": bool(current)}


class ScoreStore:
    def __init__(self):
        self.scores, self.hashes, self.rounds = defaultdict(dict), defaultdict(dict), defaultdict(set)
        self.expiries = {}
        self.eval = AsyncMock(side_effect=self.apply)
        self.zrevrange = AsyncMock(side_effect=self.ranking)
        self.hget = AsyncMock(side_effect=lambda key, uid: self.hashes[key].get(str(uid)))

    async def apply(self, script, numkeys, *args):
        assert numkeys == 4
        key, names, usernames, rounds, expires, token, *players = args
        if token in self.rounds[rounds]:
            return 0
        for index in range(0, len(players), 4):
            uid, name, username, delta = players[index : index + 4]
            self.scores[key][uid] = max(0, self.scores[key].get(uid, 0) + delta)
            self.hashes[names][uid], self.hashes[usernames][uid] = name, username
        self.rounds[rounds].add(token)
        self.expiries.update(dict.fromkeys((key, names, usernames, rounds), expires))
        return 1

    async def ranking(self, key, first, last, *, withscores):
        assert withscores
        return sorted(self.scores[key].items(), key=lambda item: item[1], reverse=True)[first : last + 1]


class GameSession(RecordingSession):
    def __init__(self, backend):
        super().__init__()
        self.backend, self.messages, self.timeouts = backend, {}, []
        self.photo_hook, self.edit_hook = None, None
        self.media_error, self.caption_error = False, False

    async def make_request(self, bot, method, timeout=None):
        self.timeouts.append(timeout)
        if isinstance(method, SendPhoto) and self.photo_hook:
            await self.photo_hook(method)
        if isinstance(method, (SendMessage, SendPhoto)):
            self.methods.append(method)
            message = make_message(
                bot,
                message_id=100 + len(self.messages),
                date=self.backend.now,
                chat={"id": method.chat_id, "type": "supergroup"},
                message_thread_id=method.message_thread_id,
                is_topic_message=method.message_thread_id is not None,
                from_user={"id": bot.id, "is_bot": True, "first_name": "Bot"},
                photo=[{"file_id": "photo", "file_unique_id": "unique", "width": 960, "height": 768}]
                if isinstance(method, SendPhoto)
                else None,
                reply_markup=None if method.reply_markup is None else method.reply_markup.model_dump(mode="json"),
            )
            self.messages[message.message_id] = message
            return message
        if isinstance(method, (EditMessageCaption, EditMessageMedia)):
            self.methods.append(method)
            if self.edit_hook:
                await self.edit_hook(method)
            if (isinstance(method, EditMessageMedia) and self.media_error) or (
                isinstance(method, EditMessageCaption) and self.caption_error
            ):
                raise TelegramBadRequest(method=method, message="message can't be edited")
            return self.messages[method.message_id]
        return await super().make_request(bot, method, timeout)


@pytest.fixture(params=["chess", "geoguess"])
async def rig(request, monkeypatch):
    monkeypatch.setattr("msu_hub_bot.games.quiz.EDIT_INTERVAL", 0)
    monkeypatch.setattr(definitions, "random_puzzle", AsyncMock(return_value=PUZZLE))
    monkeypatch.setattr(definitions, "random_photo", AsyncMock(return_value=PHOTO))
    monkeypatch.setattr(definitions, "render_board", Mock(return_value=PNG))
    backend = FeatureFixture()
    session = GameSession(backend)
    bot = BotWrapper("123456789:" + "a" * 35, session=session)
    client = ScoreStore()
    result = SimpleNamespace(
        feature=request.param,
        backend=backend,
        session=session,
        bot=bot,
        client=client,
        redis=SimpleNamespace(redis=AsyncMock(return_value=client)),
        message=make_message(bot, message_id=10, date=backend.now, is_topic_message=True, message_thread_id=17),
    )
    restart(result)
    yield result
    result.worker.stop()
    await session.close()


def restart(rig):
    rig.store = FeatureStore(rig.backend)
    rig.worker = FeatureWorker(rig.store)
    rig.quiz = QuizService(rig.bot, rig.redis, rig.store, rig.worker)
    rig.quiz.clock = lambda: rig.backend.now


async def settle(rig, *, attempts=10):
    for _ in range(attempts):
        if not await rig.worker.run_once():
            return


async def start(rig, message=None):
    message = message or rig.message
    await rig.quiz.start(rig.feature, message)
    token = rig.quiz._token(rig.bot.id, message.chat.id, message.message_id)
    return await rig.quiz.round(rig.feature, message.chat.id, token)


async def click(rig, record, choice, *, user_id=42, token=None, message=None):
    message = message or rig.session.messages[record.value.message_id]
    query = CallbackQuery.model_validate(
        {
            "id": str(uuid4()),
            "chat_instance": "synthetic",
            "message": message,
            "data": f"{rig.feature}:{token or record.key}:{choice}",
            "from_user": {"id": user_id, "is_bot": False, "first_name": f"User {user_id} <&>", "username": f"user_{user_id}"},
        },
        context={"bot": rig.bot},
    )
    await rig.quiz.callback(rig.feature, query, token or record.key, str(choice))
    return query


def text_of(method):
    if isinstance(method, EditMessageMedia):
        return method.media.caption or ""
    return getattr(method, "caption", None) or getattr(method, "text", None) or ""


def edits(rig):
    return [method for method in rig.session.methods if isinstance(method, (EditMessageCaption, EditMessageMedia))]
