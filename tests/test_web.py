"""Authenticated Mini App contracts over local Unix sockets, never external network."""

import asyncio
import hmac
import json
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from urllib.parse import urlencode
from uuid import uuid4

import aiohttp
import pytest
from aiohttp import web
from aiogram.methods import GetChatMember, SendMessage
from aiogram.types import ChatMemberLeft, ChatMemberMember, User

from msu_hub_bot.reminders import ReminderService, Schedule
from msu_hub_bot.storage.errors import RepositoryFailure, RepositoryUnavailable
from msu_hub_bot.storage.features import FeatureStore, FeatureWorker
from msu_hub_bot.telemetry import Telemetry
from msu_hub_bot.web.auth import AuthenticationError, authenticate
from msu_hub_bot.web.links import Destination, LAUNCH_SECONDS, WebAppLinks
from msu_hub_bot.web.server import WebServer
from quiz_helpers import FeatureFixture
from telegram_helpers import make_bot, make_message

NOW = datetime(2030, 1, 1, 10, tzinfo=UTC)
TOKEN = "123456789:" + "a" * 35


def signed(*, user=None, age=0, fields=None):
    data = {"auth_date": str(int(NOW.timestamp()) - age), "user": json.dumps(user or {"id": 42, "first_name": "Owner <&>"})}
    data.update(fields or {})
    secret = hmac.digest(b"WebAppData", TOKEN.encode(), "sha256")
    data["hash"] = hmac.digest(secret, "\n".join(f"{key}={value}" for key, value in sorted(data.items())).encode(), "sha256").hex()
    return urlencode(data)


def test_current_telegram_signature_accepts_real_shape_and_bounds_profile_name():
    user = authenticate(signed(user={"id": 42, "first_name": "A" * 256, "last_name": "B" * 256}), TOKEN, now=NOW)
    assert user.id == 42 and len(user.name) == 256 and user.name.endswith("…")


@pytest.mark.parametrize(
    "value",
    [
        "",
        "hash=bad",
        signed(age=3601),
        signed(age=-31),
        signed(user={"id": True, "first_name": "No"}),
        signed(user={"id": 0, "first_name": "No"}),
        signed(user={"id": 42, "first_name": "No", "is_bot": True}),
        signed() + "&user=",
        signed() + "&auth_date=1",
        signed().replace("Owner", "Other"),
        signed(fields={"user": '{"id":42,"id":43,"first_name":"No"}'}),
        signed(fields={"user": "null"}),
        signed(fields={"auth_date": "NaN"}),
        signed() + "&extra=" + "a" * 17000,
    ],
)
def test_auth_rejects_tampered_stale_ambiguous_or_malformed_data(value):
    with pytest.raises(AuthenticationError) as error:
        authenticate(value, TOKEN, now=NOW)
    assert "Owner" not in str(error.value) and TOKEN not in str(error.value)


@pytest.mark.parametrize("age", [-30, 0, 3600])
def test_auth_freshness_boundaries(age):
    assert authenticate(signed(age=age), TOKEN, now=NOW).id == 42


def test_launch_context_is_user_bound_short_expiring_and_supports_private_topics():
    links = WebAppLinks(TOKEN, "https://app.example")
    group = Destination(-123, 17)
    token = links.launch(42, group, now=NOW)
    assert len(token) == 43 and len("app_" + token) <= 64
    assert links.destination(42, token, now=NOW) == group
    assert links.destination(42, None, now=NOW) == Destination(42)
    private_topic = links.launch(42, Destination(42, 17), now=NOW)
    assert links.destination(42, private_topic, now=NOW) == Destination(42, 17)
    for user, value, time in [(43, token, NOW), (42, token, NOW + timedelta(seconds=LAUNCH_SECONDS + 1)), (42, "A" + token[1:], NOW)]:
        with pytest.raises(ValueError):
            links.destination(user, value, now=time)
    different_bot = WebAppLinks(TOKEN + "z", "https://app.example")
    with pytest.raises(ValueError):
        different_bot.destination(42, token, now=NOW)


def test_launch_buttons_keep_private_only_webapp_protocol_and_request_ids_separate():
    links = WebAppLinks(TOKEN, "https://app.example")
    links.username = "test_bot"
    group = links.button(make_message(), now=NOW)
    assert group.web_app is None and group.url.startswith("https://t.me/test_bot?start=app_")
    private = links.button(make_message(chat={"id": 42, "type": "private"}), now=NOW)
    assert private.web_app.url.startswith("https://app.example/?launch=")
    assert links.request_message_id(42, "request") == links.request_message_id(42, "request") < 0
    assert links.request_message_id(43, "request") != links.request_message_id(42, "request")


def test_anonymous_and_bot_messages_never_mint_personal_launch_links():
    links = WebAppLinks(TOKEN, "https://app.example")
    links.username = "test_bot"
    anonymous = make_message(sender_chat={"id": -123, "type": "supergroup", "title": "Synthetic"})
    bot_message = make_message(from_user={"id": 42, "is_bot": True, "first_name": "Bot"})
    assert links.button(anonymous, now=NOW) is None
    assert links.button(bot_message, now=NOW) is None


@pytest.mark.parametrize(
    "url", ["http://app.example", "https://secret@app.example", "https://app.example/?token=secret", "https://app.example/#secret"]
)
def test_launch_url_configuration_rejects_embedded_credentials(url):
    with pytest.raises(ValueError):
        WebAppLinks(TOKEN, url)


@pytest.fixture
async def rig(monkeypatch):
    backend = FeatureFixture()
    backend.now = NOW
    bot = make_bot()
    original = bot.session.make_request
    membership = {"allowed": True}

    async def request(bot, method, timeout=None):
        if isinstance(method, GetChatMember):
            bot.session.methods.append(method)
            user = User(id=method.user_id, first_name="Synthetic", is_bot=False)
            return ChatMemberMember(user=user) if membership["allowed"] else ChatMemberLeft(user=user)
        return await original(bot, method, timeout)

    monkeypatch.setattr(bot.session, "make_request", request)
    store = FeatureStore(backend)
    reminders = ReminderService(bot, store, FeatureWorker(store))
    reminders.clock = lambda: backend.now
    database = SimpleNamespace(get_chat=AsyncMock(return_value=SimpleNamespace(full_name="Synthetic chat")))
    links = WebAppLinks(TOKEN, "https://app.example")
    with tempfile.TemporaryDirectory(prefix="hubweb-", dir="/tmp") as directory:
        assets = Path(directory)
        (assets / "index.html").write_text("<html><body>Synthetic app</body></html>")
        (assets / "assets").mkdir()
        (assets / "assets" / "app.js").write_text("document.body.dataset.loaded = 'yes';")
        server = WebServer(bot, reminders, database, links, Telemetry(), static_path=assets)
        server.clock = lambda: backend.now
        runner = web.AppRunner(server.application(), access_log=None)
        server.runner = runner
        await runner.setup()
        socket = str(assets / "http.sock")
        await web.UnixSite(runner, socket).start()
        async with aiohttp.ClientSession(connector=aiohttp.UnixConnector(path=socket)) as client:

            async def api(method, path, *, user_id=42, body=None, headers=None, data=None):
                request_headers = {"Authorization": "tma " + signed(user={"id": user_id, "first_name": "Owner <&>"}), "Origin": links.url}
                request_headers.update(headers or {})
                return await client.request(method, "http://local" + path, json=body, data=data, headers=request_headers)

            yield SimpleNamespace(
                api=api, client=client, backend=backend, bot=bot, reminders=reminders, server=server, links=links, membership=membership
            )
        await server.close()
        await bot.session.close()


def creation(**changes):
    return {"request_id": str(uuid4()), "text": "30m meeting <&>", "schedule": "in 1h", "timezone": "Europe/Moscow", **changes}


async def test_web_telemetry_exports_verified_identity_without_request_contents(rig, monkeypatch):
    from telemetry_helpers import Capture, config

    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
    capture = Capture()
    telemetry = Telemetry(config(), transport=capture)
    rig.server.telemetry = telemetry
    await telemetry.start()
    try:
        response = await rig.api("POST", "/api/reminders", body=creation(text="private-reminder-canary"))
        assert response.status == 201
    finally:
        await telemetry.close()
    spans = [span for span in capture.spans() if span.name == "web.request"]
    assert len(spans) == 1
    assert any(item.key == "telegram.user_id" and item.value.int_value == 42 for item in spans[0].attributes)
    serialized = capture.serialized()
    for private in (TOKEN, "Owner <&>", "private-reminder-canary", "Authorization", signed(), "app.example"):
        assert private not in serialized


def assert_headers(response):
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert "frame-ancestors" in response.headers["Content-Security-Policy"]
    assert response.headers["Cache-Control"] == "no-store"


async def test_http_auth_origin_and_shutdown_errors_all_keep_security_headers(rig):
    for headers, status in [
        ({"Authorization": ""}, 401),
        ({"Authorization": "tma broken"}, 401),
        ({"Origin": "https://other.example"}, 403),
    ]:
        response = await rig.api("GET", "/api/session", headers=headers)
        assert response.status == status
        assert_headers(response)
    rig.server.accepting = False
    response = await rig.api("GET", "/api/session")
    assert response.status == 503
    assert_headers(response)
    assert not rig.backend.records and not rig.bot.session.methods


async def test_private_session_and_assets_do_not_need_browser_credentials_for_static_files(rig):
    response = await rig.api("GET", "/api/session")
    assert (await response.json())["context"] == {"chat_id": 42, "thread_id": None, "label": "Личные сообщения"}
    response = await rig.client.get("http://local/")
    assert response.status == 200 and "Synthetic app" in await response.text()
    assert response.headers["Cache-Control"] == "no-cache"
    assert (await rig.client.get("http://local/assets/app.js")).status == 200
    assert (await rig.client.get("http://local/assets/../auth.py")).status == 404


async def test_create_keeps_time_and_text_separate_and_confirms_once_under_concurrent_retry(rig):
    body = creation()
    responses = await asyncio.gather(*(rig.api("POST", "/api/reminders", body=body) for _ in range(4)))
    results = [await response.json() for response in responses]
    assert sorted(response.status for response in responses) == [200, 200, 200, 201]
    assert len({item["key"] for item in results}) == 1
    assert all(item["text"] == body["text"] and item["due_at"] == "2030-01-01T11:00:00Z" for item in results)
    assert "destination_label" not in results[0]
    sends = [method for method in rig.bot.session.methods if isinstance(method, SendMessage)]
    assert len(sends) == 1 and sends[0].reply_markup and sends[0].reply_parameters is None
    assert sends[0].chat_id == 42 and "30m meeting <&>" in sends[0].text


async def test_create_replay_after_due_time_does_not_reparse_schedule(rig):
    body = creation(schedule="at 2030-01-01 10:01 [UTC]")
    first = await (await rig.api("POST", "/api/reminders", body=body)).json()
    rig.backend.now += timedelta(minutes=2)
    response = await rig.api("POST", "/api/reminders", body=body)
    assert response.status == 200 and (await response.json())["key"] == first["key"]
    assert len([m for m in rig.bot.session.methods if isinstance(m, SendMessage)]) == 1


async def test_group_creation_requires_signed_destination_and_current_membership(rig):
    token = rig.links.launch(42, Destination(-123, 17), now=NOW)
    rig.membership["allowed"] = False
    response = await rig.api("POST", "/api/reminders", body=creation(launch=token))
    assert response.status == 403 and not rig.backend.records
    rig.membership["allowed"] = True
    response = await rig.api("POST", "/api/reminders", body=creation(launch=token))
    assert response.status == 201
    item = await response.json()
    assert (item["chat_id"], item["thread_id"]) == (-123, 17)
    sends = [method for method in rig.bot.session.methods if isinstance(method, SendMessage)]
    assert len(sends) == 1 and sends[0].message_thread_id == 17
    response = await rig.api("POST", "/api/reminders", user_id=43, body=creation(launch=token))
    assert response.status == 422 and len(rig.backend.records) == 1


async def test_expired_launch_explains_how_to_reopen_without_creating_a_record(rig):
    token = rig.links.launch(42, Destination(-123, 17), now=NOW)
    rig.backend.now += timedelta(seconds=LAUNCH_SECONDS + 1)
    auth = signed(fields={"auth_date": str(int(rig.backend.now.timestamp()))})
    response = await rig.api("POST", "/api/reminders", body=creation(launch=token), headers={"Authorization": "tma " + auth})
    result = await response.json()
    assert response.status == 422 and result["error"]["code"] == "launch"
    assert "исходного чата" in result["error"]["message"]
    assert not rig.backend.records and not rig.bot.session.methods


@pytest.mark.parametrize("action", ["reschedule", "retry"])
async def test_removed_owner_cannot_change_or_retry_group_reminder_but_can_cancel(rig, action):
    token = rig.links.launch(42, Destination(-123, 17), now=NOW)
    item = await (await rig.api("POST", "/api/reminders", body=creation(launch=token))).json()
    if action == "retry":
        record = await rig.reminders.get(42, item["key"])
        await rig.reminders._terminal(record, "uncertain", failure="uncertain")
        item = await (await rig.api("GET", f"/api/reminders/{item['key']}")).json()
    rig.membership["allowed"] = False
    body = {"etag": item["etag"], **({"schedule": "in 2h", "text": "Changed text"} if action == "reschedule" else {})}
    response = await rig.api("POST", f"/api/reminders/{item['key']}/{action}", body=body)
    assert response.status == 403 and (await response.json())["error"]["code"] == "membership"
    current = await rig.reminders.get(42, item["key"])
    assert current.value.text == item["text"] and current.etag == item["etag"]
    membership_calls = len([method for method in rig.bot.session.methods if isinstance(method, GetChatMember)])
    response = await rig.api("POST", f"/api/reminders/{item['key']}/cancel", body={"etag": item["etag"]})
    assert response.status == 200 and (await response.json())["status"] == "cancelled"
    assert len([method for method in rig.bot.session.methods if isinstance(method, GetChatMember)]) == membership_calls
    assert len([method for method in rig.bot.session.methods if isinstance(method, SendMessage)]) == 1


async def test_list_get_and_changes_cannot_access_another_authors_record(rig):
    item = await (await rig.api("POST", "/api/reminders", body=creation())).json()
    response = await rig.api("GET", "/api/reminders", user_id=43)
    assert (await response.json())["items"] == []
    for method, path, body in [
        ("GET", f"/api/reminders/{item['key']}", None),
        ("POST", f"/api/reminders/{item['key']}/cancel", {"etag": item["etag"]}),
        ("POST", f"/api/reminders/{item['key']}/reschedule", {"etag": item["etag"], "schedule": "in 2h"}),
    ]:
        response = await rig.api(method, path, user_id=43, body=body)
        assert response.status == 422 and "meeting" not in await response.text()
    assert (await rig.reminders.get(42, item["key"])).value.status == "pending"


async def test_reschedule_uses_etag_preserves_text_and_cancel_does_not_send_again(rig):
    item = await (await rig.api("POST", "/api/reminders", body=creation())).json()
    path = f"/api/reminders/{item['key']}"
    response = await rig.api("POST", path + "/reschedule", body={"etag": item["etag"], "schedule": "in 2h"})
    updated = await response.json()
    assert response.status == 200 and updated["text"] == item["text"] and updated["due_at"] == "2030-01-01T12:00:00Z"
    assert (await rig.api("POST", path + "/cancel", body={"etag": item["etag"]})).status == 409
    response = await rig.api("POST", path + "/cancel", body={"etag": updated["etag"]})
    assert (await response.json())["status"] == "cancelled"
    assert len([m for m in rig.bot.session.methods if isinstance(m, SendMessage)]) == 1


async def test_revision_race_after_http_read_still_returns_conflict(rig, monkeypatch):
    item = await (await rig.api("POST", "/api/reminders", body=creation())).json()
    original = rig.reminders.cancel

    async def racing_cancel(author_id, key, **kwargs):
        await rig.reminders.reschedule(author_id, key, Schedule(due_at=NOW + timedelta(hours=3)))
        return await original(author_id, key, **kwargs)

    monkeypatch.setattr(rig.reminders, "cancel", racing_cancel)
    response = await rig.api("POST", f"/api/reminders/{item['key']}/cancel", body={"etag": item["etag"]})
    assert response.status == 409 and (await response.json())["error"]["code"] == "conflict"
    assert (await rig.reminders.get(42, item["key"])).value.status == "pending"


async def test_listing_resolves_unique_destinations_with_bounded_parallel_queries(rig, monkeypatch):
    for index in range(20):
        await rig.reminders.create(
            author_id=42,
            author_name="Owner",
            chat_id=-1000 - index // 2,
            thread_id=17,
            source_message_id=index,
            schedule=Schedule(due_at=NOW + timedelta(hours=1), text="Synthetic"),
        )
    active, peak, calls = 0, 0, []
    release = asyncio.Event()

    async def lookup(chat_id):
        nonlocal active, peak
        calls.append(chat_id)
        active += 1
        peak = max(peak, active)
        if active == 8:
            release.set()
        try:
            await release.wait()
            await asyncio.sleep(0)
            return SimpleNamespace(full_name=f"Chat {chat_id}")
        finally:
            active -= 1

    monkeypatch.setattr(rig.server.database, "get_chat", lookup)
    async with asyncio.timeout(2):
        response = await rig.api("GET", "/api/reminders?limit=100")
    items = (await response.json())["items"]
    assert len(items) == 20 and len(calls) == len(set(calls)) == 10
    assert peak == 8 and active == 0
    assert all(item["destination_label"] == f"Chat {item['chat_id']} · тема 17" for item in items)


async def test_label_storage_failure_returns_unavailable_and_cancels_remaining_queries(rig, monkeypatch):
    for index in range(10):
        await rig.reminders.create(
            author_id=42,
            author_name="Owner",
            chat_id=-1000 - index,
            thread_id=None,
            source_message_id=index,
            schedule=Schedule(due_at=NOW + timedelta(hours=1), text="Synthetic"),
        )
    active, calls = 0, 0

    async def lookup(chat_id):
        nonlocal active, calls
        calls += 1
        if calls == 1:
            raise RepositoryUnavailable(RepositoryFailure.UNAVAILABLE)
        active += 1
        try:
            await asyncio.Event().wait()
        finally:
            active -= 1

    monkeypatch.setattr(rig.server.database, "get_chat", lookup)
    response = await rig.api("GET", "/api/reminders")
    assert response.status == 503 and active == 0


async def test_confirmation_failure_is_best_effort_and_never_replayed(rig, monkeypatch, caplog):
    async def fail(bot, method, timeout=None):
        raise TimeoutError("SECRET reminder acknowledgement")

    monkeypatch.setattr(rig.bot.session, "make_request", fail)
    body = creation()
    response = await rig.api("POST", "/api/reminders", body=body)
    assert response.status == 201
    monkeypatch.setattr(rig.server, "_confirmation", AsyncMock(side_effect=AssertionError("must not resend")))
    response = await rig.api("POST", "/api/reminders", body=body)
    assert response.status == 200
    assert "SECRET" not in caplog.text and body["text"] not in caplog.text


async def test_lost_database_response_recovers_same_created_record(rig, monkeypatch):
    original = rig.backend.feature_request
    failures = 0

    async def uncertain(operation, request):
        nonlocal failures
        result = await original(operation, request)
        if operation == "commit" and failures < 2:
            failures += 1
            raise RepositoryUnavailable(RepositoryFailure.UNAVAILABLE)
        return result

    monkeypatch.setattr(rig.backend, "feature_request", uncertain)
    body = creation()
    assert (await rig.api("POST", "/api/reminders", body=body)).status == 503
    response = await rig.api("POST", "/api/reminders", body=body)
    assert response.status == 200 and len(rig.backend.records) == 1
    assert not rig.bot.session.methods


@pytest.mark.parametrize(
    "changes", [{"schedule": "in 1h trailing text"}, {"text": "😀" * 1501}, {"timezone": "Missing/Zone"}, {"chat_id": -999}]
)
async def test_invalid_input_never_creates_or_confirms(rig, changes):
    response = await rig.api("POST", "/api/reminders", body=creation(**changes))
    assert response.status == 422 and not rig.backend.records and not rig.bot.session.methods


async def test_json_duplicates_oversized_bodies_and_unexpected_errors_are_sanitized(rig, monkeypatch, caplog):
    response = await rig.api(
        "POST", "/api/reminders", headers={"Content-Type": "application/json"}, data='{"request_id":"a","request_id":"b"}'
    )
    assert response.status == 422
    response = await rig.api("POST", "/api/reminders", headers={"Content-Type": "application/json"}, data="x" * 33000)
    assert response.status == 413
    monkeypatch.setattr(rig.reminders, "list", AsyncMock(side_effect=RuntimeError("SECRET-TEXT")))
    response = await rig.api("GET", "/api/reminders")
    assert response.status == 500
    assert "SECRET-TEXT" not in await response.text() and "SECRET-TEXT" not in caplog.text


async def test_request_deadline_cancels_blocked_read_and_close_drains_running_request(rig, monkeypatch):
    import msu_hub_bot.web.server as module

    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def blocked(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(rig.reminders, "list", blocked)
    monkeypatch.setattr(module, "REQUEST_TIMEOUT", 0.02)
    response = await rig.api("GET", "/api/reminders")
    assert response.status == 503 and entered.is_set() and cancelled.is_set()
    monkeypatch.setattr(module, "REQUEST_TIMEOUT", 2)
    entered.clear()
    release = asyncio.Event()

    async def finish(*args, **kwargs):
        entered.set()
        await release.wait()
        return []

    monkeypatch.setattr(rig.reminders, "list", finish)
    pending = asyncio.create_task(rig.api("GET", "/api/reminders"))
    await entered.wait()
    closing = asyncio.create_task(rig.server.close())
    await asyncio.sleep(0)
    assert not closing.done() and not rig.server.accepting
    release.set()
    assert (await pending).status == 200
    await closing
    assert rig.server.runner is None


async def test_listener_start_failure_closes_partial_runner(rig, monkeypatch):
    import msu_hub_bot.web.server as module

    server = WebServer(rig.bot, rig.reminders, rig.server.database, rig.links, Telemetry(), static_path=rig.server.static_path)
    runner = SimpleNamespace(setup=AsyncMock(), cleanup=AsyncMock())
    monkeypatch.setattr(module.web, "AppRunner", Mock(return_value=runner))
    monkeypatch.setattr(module.web, "TCPSite", Mock(side_effect=OSError("bind failed")))
    with pytest.raises(OSError):
        await server.start()
    runner.cleanup.assert_awaited_once()
    assert server.runner is None and not server.accepting
