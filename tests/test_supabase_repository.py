"""Supabase HTTP contracts use synthetic responses with no network access."""

import asyncio
import json
import traceback
from collections import Counter
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import aiohttp
import pytest
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.types import Chat, Message, Update, User
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest

from msu_hub_bot.storage import supabase as module
from msu_hub_bot.storage.models import ArchivedUpdate, ChatObservation, DirectoryCreate, DirectoryPatch, UserObservation, VkPatch
from msu_hub_bot.storage.observations import archive_observation
from msu_hub_bot.telegram.middlewares.settings import SettingsMiddleware
from msu_hub_bot.telegram.middlewares.updates import UpdatesMiddleware
from msu_hub_bot.telegram.runtime import AdmissionMiddleware, Supervisor
from msu_hub_bot.telemetry import Backend, Telemetry
from telemetry_helpers import Capture, config

CANARY = "synthetic-private-value"
NOW = datetime(2026, 9, 17, tzinfo=UTC)
BOT_ID = 123456789
HEALTH = {"schema_version": 1, "bot_id": BOT_ID}


def token(name="one", expires=3600):
    return {"access_token": f"access-{name}", "refresh_token": f"refresh-{name}", "expires_in": expires, "token_type": "bearer"}


def directory(chat_id=-100, **changes):
    return {
        "id": str(UUID(int=abs(chat_id))),
        "created": NOW.isoformat(),
        "chat_id": chat_id,
        "name": "Друзья",
        "section": "friends",
        "is_hidden": False,
        "username_alias": None,
        "members": None,
        "pinned_message_id": None,
        **changes,
    }


def chat(**changes):
    return {"id": str(UUID(int=1)), "created": NOW.isoformat(), "chat_id": -100, "type": "supergroup", "metadata": {}, **changes}


def subscription(**changes):
    return {
        "id": str(UUID(int=2)),
        "created": NOW.isoformat(),
        "owner_id": -200,
        "chat_id": -100,
        "last_post_id": 0,
        "with_reposts": False,
        "with_header": True,
        "is_suspended": False,
        **changes,
    }


class Response:
    def __init__(self, value=None, *, status=200, raw=None, content_type="application/json", gate=None, delay=0):
        self.status = status
        self.body = json.dumps(value).encode() if raw is None else raw
        self.content_type = content_type
        self.content = self
        self.gate = gate
        self.delay = delay
        self.closed = False
        self.read = False
        self.cancelled = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    async def iter_chunked(self, size):
        self.read = True
        try:
            if self.gate is not None:
                await self.gate.wait()
            await asyncio.sleep(self.delay)
            for offset in range(0, len(self.body), size):
                yield self.body[offset : offset + size]
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.options = None
        self.closed = False

    def factory(self, **options):
        self.options = options
        return self

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        result = self.responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    async def close(self):
        self.closed = True


@pytest.fixture
def configured(monkeypatch):
    repositories = []

    def create(responses, **options):
        session = Session(responses)
        monkeypatch.setattr(module.aiohttp, "ClientSession", session.factory)
        config = SimpleNamespace(
            bot_token=f"{BOT_ID}:{CANARY}",
            supabase_url="http://supabase.invalid:8000",
            supabase_key=f"publishable-{CANARY}",
            supabase_email="bot@example.invalid",
            supabase_password=CANARY,
            supabase_schema="msu_hub_api",
        )
        repo = module.SupabaseRepository(config, **options)
        repositories.append(repo)
        return repo, session

    yield create
    assert all(repo._session.closed for repo in repositories), "Each test must close its owned HTTP pool"


@pytest.mark.parametrize("change_settings", [False, True])
async def test_middleware_and_api_telemetry_have_distinct_owners(configured, monkeypatch, change_settings):
    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
    sink = Capture()
    telemetry = Telemetry(config(), transport=sink)
    responses = [Response(token()), Response({})]
    if change_settings:
        responses.append(Response({"with_nsfw": True}))
    responses.append(Response(None, status=204, raw=b""))
    repo, _ = configured(responses, telemetry=telemetry)
    preferences = SettingsMiddleware(repo, telemetry=telemetry, backend=Backend.SUPABASE)
    supervisor = Supervisor(telemetry)
    archive = UpdatesMiddleware(repo, supervisor, telemetry=telemetry, backend=Backend.SUPABASE)
    update = Update(
        update_id=1,
        message=Message(
            message_id=1,
            date=NOW,
            chat=Chat(id=-100, type="supergroup", title=CANARY),
            from_user=User(id=101, is_bot=False, first_name=CANARY),
            text=CANARY,
        ),
    )

    async def handler(message, data):
        if change_settings:
            data["settings"].with_nsfw = True
            return None
        return UNHANDLED

    async def dispatch(event, data):
        return await preferences(handler, event.message, data)

    async def admitted(event, data):
        return await archive(dispatch, event, data)

    await telemetry.start()
    try:
        await asyncio.create_task(AdmissionMiddleware(supervisor)(admitted, update, {}))
        await supervisor.drain(1, cancel_timeout=0.1)
    finally:
        await preferences.close()
        await repo.close()
        await telemetry.close()

    counts = Counter()
    for message in sink.messages():
        if isinstance(message, ExportMetricsServiceRequest):
            for resource in message.resource_metrics:
                for scope in resource.scope_metrics:
                    for metric in scope.metrics:
                        if metric.name == "bot.operations":
                            for point in metric.sum.data_points:
                                operation = next(attr.value.string_value for attr in point.attributes if attr.key == "operation")
                                counts[operation] += point.as_int
    assert counts["settings.load"] == counts["archive.write"] == counts["database.read"] == counts["database.auth"] == 1
    assert counts["settings.save"] == int(change_settings)
    assert counts["database.write"] == 1 + int(change_settings)
    spans = sink.spans()
    if change_settings:
        by_operation = {next(attr.value.string_value for attr in span.attributes if attr.key == "operation"): span for span in spans}
        assert set(by_operation) == {"settings.save", "database.write"}
        assert by_operation["database.write"].parent_span_id == by_operation["settings.save"].span_id
    else:
        assert spans == []
    assert CANARY not in sink.serialized()


async def test_expired_message_body_never_enters_archive_request(configured):
    repo, session = configured([Response(token()), Response(None, status=204, raw=b"")])
    update = archive_observation(
        Update(
            update_id=1,
            message=Message(message_id=1, date=NOW - timedelta(days=31), chat=Chat(id=-100, type="supergroup"), text=CANARY),
        ),
        True,
        received_at=NOW,
    )
    try:
        await repo.archive_update(update)
        payload = session.calls[-1][1]["json"]["p_update"]
        assert payload["data"]["message"]["message_id"] == 1
        assert payload["messages"] == []
        assert CANARY not in json.dumps(payload)
        assert CANARY not in repr(update)
    finally:
        await repo.close()


async def test_password_auth_health_reuses_token_and_scopes_requests(configured):
    repo, session = configured([Response(token()), Response(HEALTH), Response(None)])
    try:
        await repo.check()
        assert await repo.get_chat(-100) is None
        auth_url, auth = session.calls[0]
        assert auth_url.endswith("/auth/v1/token?grant_type=password")
        assert auth["json"] == {"email": "bot@example.invalid", "password": CANARY}
        assert "Authorization" not in auth["headers"]
        _, rpc = session.calls[1]
        assert rpc["headers"]["Authorization"] == "Bearer access-one"
        assert rpc["headers"]["Content-Profile"] == rpc["headers"]["Accept-Profile"] == "msu_hub_api"
        assert rpc["json"] == {}
        assert all(call[1]["allow_redirects"] is False for call in session.calls)
        assert session.options["trust_env"] is False
        assert isinstance(session.options["cookie_jar"], aiohttp.DummyCookieJar)
        assert CANARY not in repr(repo) + repr(repo._token)
    finally:
        await repo.close()


async def test_concurrent_auth_and_refresh_are_serialized(configured):
    gate = asyncio.Event()
    first = Response(token(), gate=gate)
    repo, session = configured([first, Response(HEALTH), Response(HEALTH), Response(token("two")), Response(HEALTH), Response(HEALTH)])
    try:
        tasks = [asyncio.create_task(repo.check()) for _ in range(2)]
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert len(session.calls) == 1
        gate.set()
        await asyncio.gather(*tasks)
        repo._refresh_at = 0
        await asyncio.gather(repo.check(), repo.check())
        grants = [call for call in session.calls if "/auth/v1/" in call[0]]
        assert len(grants) == 2
        assert grants[1][0].endswith("grant_type=refresh_token")
        assert grants[1][1]["json"] == {"refresh_token": "refresh-one"}
        assert session.calls[-1][1]["headers"]["Authorization"] == "Bearer access-two"
    finally:
        await repo.close()


async def test_lost_refresh_response_is_not_replayed(configured):
    repo, session = configured(
        [Response(token()), Response(HEALTH), aiohttp.ServerDisconnectedError(CANARY), Response(token("new")), Response(HEALTH)]
    )
    try:
        await repo.check()
        repo._refresh_at = 0
        with pytest.raises(module.RepositoryUnavailable):
            await repo.check()
        assert len(session.calls) == 3
        repo._auth_retry_at = 0
        await repo.check()
        assert session.calls[3][0].endswith("grant_type=password")
    finally:
        await repo.close()


async def test_failed_auth_does_not_stampede_from_queued_updates(configured):
    repo, session = configured([Response({"message": CANARY}, status=400)])
    try:
        results = await asyncio.gather(*(repo.check() for _ in range(10)), return_exceptions=True)
        assert all(isinstance(result, module.RepositoryError) for result in results)
        assert len(session.calls) == 1
    finally:
        await repo.close()


@pytest.mark.parametrize(
    "changes",
    [{"expires_in": True}, {"expires_in": "3600"}, {"expires_in": 0}, {"access_token": ""}, {"refresh_token": ""}, {"token_type": "wrong"}],
)
async def test_malformed_auth_never_sends_an_rpc(configured, changes):
    repo, session = configured([Response(token() | changes)])
    try:
        with pytest.raises(module.RepositoryProtocolError):
            await repo.check()
        assert len(session.calls) == 1
        assert repo._token is None
    finally:
        await repo.close()


@pytest.mark.parametrize("status", [301, 400, 401, 403, 409, 429, 503])
async def test_http_errors_are_safe_and_never_retry_a_write(configured, status):
    failure = Response({"message": CANARY, "details": CANARY}, status=status)
    repo, session = configured([Response(token()), failure])
    try:
        with pytest.raises(module.RepositoryError) as caught:
            await repo.patch_settings(-100, {"private": CANARY})
        assert len(session.calls) == 2
        assert caught.value.status == status
        assert CANARY not in "".join(traceback.format_exception(caught.value))
        assert not failure.read
        assert failure.closed
    finally:
        await repo.close()


async def test_lost_write_response_is_uncertain_and_not_retried(configured):
    repo, session = configured([Response(token()), aiohttp.ServerDisconnectedError(f"https://private.invalid/{CANARY}")])
    update = ArchivedUpdate(update_id=45, kind="message", handled=True, data={"text": CANARY})
    try:
        with pytest.raises(module.RepositoryUnavailable) as caught:
            await repo.archive_update(update)
        assert len(session.calls) == 2
        assert CANARY not in "".join(traceback.format_exception(caught.value))
        assert session.calls[-1][1]["json"]["p_update"]["id"] == str(update.id)
    finally:
        await repo.close()


async def test_rpc_unauthorized_refreshes_only_on_next_operation(configured):
    repo, session = configured([Response(token()), Response(None, status=401), Response(token("two")), Response(HEALTH)])
    try:
        with pytest.raises(module.RepositoryAuthError):
            await repo.delete_directory(-100)
        assert len(session.calls) == 2
        await repo.check()
        assert session.calls[2][0].endswith("grant_type=refresh_token")
    finally:
        await repo.close()


async def test_deadline_includes_authentication_and_rpc(configured):
    slow = Response(HEALTH, delay=0.07)
    repo, session = configured([Response(token(), delay=0.07), slow], operation_timeout=0.1)
    try:
        with pytest.raises(module.RepositoryUnavailable) as caught:
            await repo.check()
        assert caught.value.code is module.RepositoryFailure.TIMEOUT
        assert len(session.calls) == 2
        assert slow.cancelled and slow.closed
    finally:
        await repo.close()


async def test_deadline_includes_waiting_for_auth_lock(configured):
    repo, session = configured([], operation_timeout=0.02)
    try:
        async with repo._auth_lock:
            with pytest.raises(module.RepositoryUnavailable) as caught:
                await repo.check()
        assert caught.value.code is module.RepositoryFailure.TIMEOUT
        assert not session.calls
    finally:
        await repo.close()


async def test_cancellation_propagates_and_closes_response(configured):
    blocked = Response(token(), gate=asyncio.Event())
    repo, _ = configured([blocked])
    try:
        task = asyncio.create_task(repo.check())
        async with asyncio.timeout(1):
            while not blocked.read:
                if task.done():
                    await task
                await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert blocked.closed and blocked.cancelled
        assert repo._token is None and not repo._auth_lock.locked()
    finally:
        await repo.close()


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        {"schema_version": 2, "bot_id": BOT_ID},
        {"schema_version": True, "bot_id": BOT_ID},
        {"schema_version": 1, "bot_id": str(BOT_ID)},
        {"schema_version": 1, "bot_id": BOT_ID + 1},
    ],
)
async def test_health_rejects_wrong_schema_or_principal(configured, value):
    repo, _ = configured([Response(token()), Response(value)])
    try:
        with pytest.raises(module.RepositoryProtocolError):
            await repo.check()
    finally:
        await repo.close()


@pytest.mark.parametrize(
    "response",
    [
        Response(raw=b"not-json-" + CANARY.encode()),
        Response(raw=b'{"schema_version":NaN}'),
        Response(raw=b'{"schema_version":1e999}'),
        Response(HEALTH, content_type="text/html"),
        Response(raw=b""),
    ],
)
async def test_invalid_json_is_safe(configured, response):
    repo, _ = configured([Response(token()), response])
    try:
        with pytest.raises(module.RepositoryProtocolError) as caught:
            await repo.check()
        assert CANARY not in "".join(traceback.format_exception(caught.value))
    finally:
        await repo.close()


async def test_oversized_response_is_rejected_without_partial_results(configured):
    repo, _ = configured([Response(token()), Response([directory(-i) for i in range(1, 20)])], max_response_bytes=500)
    try:
        with pytest.raises(module.RepositoryProtocolError):
            await repo.list_directory()
    finally:
        await repo.close()


async def test_scalar_arrays_include_more_than_rest_default_row_cap(configured):
    records = [directory(-i) for i in range(1, 1006)]
    repo, session = configured([Response(token()), Response(records), Response([subscription(chat_id=-i) for i in range(1, 1006)])])
    try:
        assert len(await repo.list_directory()) == 1005
        assert len(await repo.list_vk_subscriptions()) == 1005
        assert len(session.calls) == 3
        assert all("Range" not in kwargs["headers"] for _, kwargs in session.calls)
    finally:
        await repo.close()


async def test_sparse_observations_and_patches_preserve_presence(configured):
    repo, session = configured(
        [
            Response(token()),
            Response({"unknown": [1, None]}),
            Response(chat()),
            Response(directory()),
            Response(subscription()),
            Response(None),
        ]
    )
    try:
        assert await repo.load_settings(ChatObservation(chat_id=-100, type="supergroup", observed_at=NOW)) == {"unknown": [1, None]}
        await repo.ensure_chat(ChatObservation(chat_id=-100, type="supergroup", username=None, observed_at=NOW))
        await repo.patch_directory(-100, DirectoryPatch(username_alias=None))
        await repo.upsert_vk_subscription(-200, -100, VkPatch(description=None))
        update = ArchivedUpdate(
            update_id=45, kind="message", handled=True, data={}, users=[UserObservation(user_id=1, is_bot=False, first_name="Name")]
        )
        await repo.archive_update(update)
        sparse = session.calls[1][1]["json"]["p_chat"]
        assert sparse == {"chat_id": -100, "type": "supergroup", "observed_at": NOW.isoformat()}
        assert session.calls[2][1]["json"]["p_chat"]["username"] is None
        assert session.calls[3][1]["json"]["p_changes"] == {"username_alias": None}
        assert session.calls[4][1]["json"]["p_changes"] == {"description": None}
        archived = session.calls[5][1]["json"]["p_update"]
        assert archived["id"] == str(update.id) and "received_at" in archived
        assert "username" not in archived["users"][0] and "observed_at" in archived["users"][0]
        assert not any("bot_id" in kwargs["json"] for _, kwargs in session.calls)
    finally:
        await repo.close()


async def test_remaining_repository_operations_return_typed_values(configured):
    repo, session = configured(
        [
            Response(token()),
            Response(chat(metadata=["legacy", None])),
            Response(directory()),
            Response(directory()),
            Response(True),
            Response(None, status=204, raw=b""),
            Response({"users": 2, "chats": 1, "updates": 10, "handled_updates": 4}),
        ]
    )
    try:
        assert (await repo.get_chat(-100)).metadata == ["legacy", None]
        assert (await repo.get_directory(-100)).name == "Друзья"
        assert (await repo.create_directory(DirectoryCreate(chat_id=-100, name="Друзья"))).chat_id == -100
        assert await repo.delete_directory(-100) is True
        await repo.advance_vk_cursor(-200, -100, 2**40)
        assert (await repo.statistics(NOW)).handled_updates == 4
        assert session.calls[-2][1]["json"]["p_last_post_id"] == 2**40
        assert session.calls[-1][1]["json"] == {"p_since": NOW.isoformat()}
    finally:
        await repo.close()


@pytest.mark.parametrize(
    "method,value",
    [("get_chat", []), ("get_directory", {}), ("list_directory", {}), ("list_vk_subscriptions", None), ("delete_directory", 1)],
)
async def test_wrong_rpc_shapes_fail_loudly(configured, method, value):
    repo, _ = configured([Response(token()), Response(value)])
    try:
        with pytest.raises(module.RepositoryProtocolError):
            await getattr(repo, method)(*(() if method.startswith("list_") else (-100,)))
    finally:
        await repo.close()


async def test_close_is_idempotent_and_prevents_new_auth(configured):
    repo, session = configured([])
    await repo.close()
    await repo.close()
    with pytest.raises(module.RepositoryUnavailable) as caught:
        await repo.check()
    assert caught.value.code is module.RepositoryFailure.CLOSED
    assert not session.calls


async def test_close_during_auth_cannot_restore_credentials(configured):
    gate = asyncio.Event()
    blocked = Response(token(), gate=gate)
    repo, session = configured([blocked])
    task = asyncio.create_task(repo.check())
    try:
        async with asyncio.timeout(1):
            while not blocked.read:
                if task.done():
                    await task
                await asyncio.sleep(0)
        await repo.close()
        gate.set()
        with pytest.raises(module.RepositoryUnavailable) as caught:
            await task
        assert caught.value.code is module.RepositoryFailure.CLOSED
        assert repo._token is None and repo._password == ""
        assert len(session.calls) == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await repo.close()
