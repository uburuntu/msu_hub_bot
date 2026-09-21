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


@pytest.mark.parametrize(
    ("failure", "status", "outcome"),
    [
        ("forbidden", 403, "rejected"),
        ("missing", 404, "rejected"),
        ("conflict", 409, "rejected"),
        ("input", 422, "rejected"),
        ("storage", 503, "unavailable"),
        ("storage_timeout", 503, "timeout"),
        ("telegram", 503, "unavailable"),
        ("timeout", 503, "timeout"),
        ("unexpected", 500, "unexpected"),
    ],
)
async def test_web_failure_telemetry_matches_the_final_http_result(rig, monkeypatch, failure, status, outcome):
    from msu_hub_bot.feedback.models import FeedbackAccessDenied, FeedbackNotFound
    from msu_hub_bot.storage.features import Conflict
    from telemetry_helpers import Capture, config

    from aiogram.exceptions import TelegramBadRequest

    private = "private-failure-content"
    errors = {
        "forbidden": FeedbackAccessDenied(),
        "missing": FeedbackNotFound(),
        "conflict": Conflict(),
        "input": ValueError(private),
        "storage": RepositoryUnavailable(RepositoryFailure.UNAVAILABLE),
        "storage_timeout": RepositoryUnavailable(RepositoryFailure.TIMEOUT),
        "telegram": TelegramBadRequest(method=GetChatMember(chat_id=-123, user_id=42), message=private),
        "timeout": TimeoutError(private),
        "unexpected": RuntimeError(private),
    }
    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
    capture = Capture()
    telemetry = Telemetry(config(), transport=capture)
    rig.server.telemetry = telemetry
    monkeypatch.setattr(rig.server.links, "destination", Mock(side_effect=errors[failure]))
    await telemetry.start()
    try:
        response = await rig.api("GET", "/api/session")
        assert response.status == status
        assert_headers(response)
    finally:
        await telemetry.close()
    spans = [span for span in capture.spans() if span.name == "web.request"]
    assert len(spans) == 1
    values = {item.key: getattr(item.value, item.value.WhichOneof("value")) for item in spans[0].attributes}
    assert values["outcome"] == outcome
    assert values["http.response.status_code"] == status
    assert private not in capture.serialized()


async def test_returned_http_errors_are_classified_without_an_exception(rig, monkeypatch):
    from telemetry_helpers import Capture, config

    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
    capture = Capture()
    telemetry = Telemetry(config(), transport=capture)
    rig.server.telemetry = telemetry
    monkeypatch.setattr(rig.server, "_label", AsyncMock(return_value="Label"))
    monkeypatch.setattr("msu_hub_bot.web.server.web.json_response", Mock(return_value=web.Response(status=503)))
    await telemetry.start()
    try:
        response = await rig.api("GET", "/api/session")
        assert response.status == 503
    finally:
        await telemetry.close()
    span = next(span for span in capture.spans() if span.name == "web.request")
    values = {item.key: getattr(item.value, item.value.WhichOneof("value")) for item in span.attributes}
    assert values["outcome"] == "unavailable"
    assert values["http.response.status_code"] == 503


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


async def _community_access(rig, monkeypatch, *, admin=True, member=True, bot_admin=True):
    from aiogram.types import ChatMemberOwner

    original = rig.bot.session.make_request

    async def request(bot, method, timeout=None):
        if isinstance(method, GetChatMember):
            rig.bot.session.methods.append(method)
            user = User(id=method.user_id, first_name="Synthetic", is_bot=method.user_id == bot.id)
            if method.user_id == bot.id:
                return ChatMemberOwner(user=user, is_anonymous=False) if bot_admin else ChatMemberMember(user=user)
            if not member:
                return ChatMemberLeft(user=user)
            return ChatMemberOwner(user=user, is_anonymous=False) if admin else ChatMemberMember(user=user)
        return await original(bot, method, timeout)

    monkeypatch.setattr(rig.bot.session, "make_request", request)
    return rig.links.launch(42, Destination(-123, 17), now=NOW)


async def test_preferences_are_owner_scoped_persistent_and_revision_guarded(rig):
    blank = await (await rig.api("GET", "/api/preferences")).json()
    assert blank == {"timezone": "Europe/Moscow", "etag": None}
    body = {"timezone": "Europe/London", "etag": None}
    response = await rig.api("PATCH", "/api/preferences", body=body)
    value = await response.json()
    assert response.status == 200 and value["timezone"] == "Europe/London"
    assert (await rig.api("PATCH", "/api/preferences", body=body)).status == 409
    assert (await (await rig.api("GET", "/api/preferences", user_id=43)).json()) == blank
    assert (await (await rig.api("GET", "/api/session")).json())["default_timezone"] == "Europe/London"
    assert (await rig.api("PATCH", "/api/preferences", body={"etag": value["etag"], "timezone": "bad/zone"})).status == 422


@pytest.mark.parametrize("admin,member,bot_admin", [(False, True, True), (True, False, True), (True, True, False)])
async def test_community_mutations_recheck_human_and_bot_admin_on_every_request(rig, monkeypatch, admin, member, bot_admin):
    launch = await _community_access(rig, monkeypatch, admin=admin, member=member, bot_admin=bot_admin)
    response = await rig.api("POST", f"/api/reposts?launch={launch}", body={"request_id": str(uuid4()), "source": "-10"})
    assert response.status == 403 and not rig.backend.records
    response = await rig.api("PATCH", f"/api/chats/-123/settings?launch={launch}", body={"etag": None, "with_nsfw": True})
    assert response.status == 403 and not rig.backend.records
    assert not any(isinstance(method, SendMessage) for method in rig.bot.session.methods)


async def test_reposts_bind_destination_preserve_cursor_and_archive_without_deleting(rig, monkeypatch):
    launch = await _community_access(rig, monkeypatch)
    body = {"request_id": str(uuid4()), "source": "https://vk.com/club10", "title": "Synthetic", "include_keywords": ["news"]}
    responses = await asyncio.gather(*(rig.api("POST", f"/api/reposts?launch={launch}", body=body) for _ in range(3)))
    assert all(response.status == 201 for response in responses)
    rows = [await response.json() for response in responses]
    item = rows[0]
    assert len({row["key"] for row in rows}) == 1
    assert item["is_suspended"] and (item["chat_id"], item["thread_id"]) == (-123, 17)
    assert (await rig.api("POST", f"/api/reposts?launch={launch}", body=body | {"title": "Changed"})).status == 409
    other_topic = rig.links.launch(42, Destination(-123, 18), now=NOW)
    path = f"/api/reposts/{item['key']}"
    assert (await rig.api("PATCH", f"{path}?launch={other_topic}", body={"etag": item["etag"], "archived": True})).status == 422
    response = await rig.api("PATCH", f"{path}?launch={launch}", body={"etag": item["etag"], "archived": True})
    changed = await response.json()
    assert changed["archived"] and changed["is_suspended"] and changed["last_post_id"] == item["last_post_id"]
    assert changed["include_keywords"] == ["news"] and len(rig.backend.records) == 2
    assert (await rig.api("PATCH", f"{path}?launch={launch}", body={"etag": changed["etag"], "is_suspended": False})).status == 422
    assert not any(isinstance(method, SendMessage) for method in rig.bot.session.methods)


async def test_launch_does_not_authorize_another_chat_or_another_users_topic(rig, monkeypatch):
    launch = await _community_access(rig, monkeypatch)
    assert (await rig.api("GET", f"/api/chats/-999/settings?launch={launch}")).status == 403
    assert (await rig.api("GET", f"/api/reposts?launch={launch}", user_id=43)).status == 422
    body = {"request_id": str(uuid4()), "source": "-10", "thread_id": 999}
    assert (await rig.api("POST", f"/api/reposts?launch={launch}", body=body)).status == 422
    assert not rig.backend.records


async def test_preview_has_no_publication_and_rejects_arbitrary_sources(rig, monkeypatch):
    launch = await _community_access(rig, monkeypatch)
    for source in ("https://internal.example/path", "https://vk.com.evil.example/club10", "https://secret@vk.com/club10"):
        assert (await rig.api("POST", f"/api/reposts/preview?launch={launch}", body={"source": source})).status == 422
    response = await rig.api("POST", f"/api/reposts/preview?launch={launch}", body={"source": "-10"})
    data = await response.json()
    assert response.status == 200 and not data["automatic_posting"] and not data["available"]
    assert not rig.backend.records and not any(isinstance(method, SendMessage) for method in rig.bot.session.methods)


async def test_web_repeat_and_saved_timezone_contract(rig):
    await rig.api("PATCH", "/api/preferences", body={"etag": None, "timezone": "Europe/London"})
    body = creation(recurrence={"kind": "daily"})
    del body["timezone"]
    response = await rig.api("POST", "/api/reminders", body=body)
    item = await response.json()
    assert response.status == 201 and item["recurrence"] == {"kind": "daily"}
    assert item["timezone"] == "Europe/London" and item["occurrences"] == item["skipped_occurrences"] == 0
    response = await rig.api(
        "POST", f"/api/reminders/{item['key']}/reschedule", body={"etag": item["etag"], "schedule": "in 2h", "recurrence": None}
    )
    item = await response.json()
    assert item["recurrence"] is None and item["timezone"] == "Europe/London"
    assert (await rig.api("POST", "/api/reminders", body=creation(recurrence={"kind": "daily", "injected": True}))).status == 422


async def test_group_repetition_requires_delivery_permissions_before_creation_or_conversion(rig):
    launch = rig.links.launch(42, Destination(-123, 17), now=NOW)
    response = await rig.api("POST", "/api/reminders", body=creation(launch=launch, recurrence={"kind": "daily"}))
    assert response.status == 422 and "администратором" in (await response.json())["error"]["message"]
    assert not rig.backend.records and not rig.backend.jobs
    assert not any(isinstance(method, SendMessage) for method in rig.bot.session.methods)

    # One-off reminders still work with an ordinary bot member.
    response = await rig.api("POST", "/api/reminders", body=creation(launch=launch))
    assert response.status == 201
    item = await response.json()
    response = await rig.api(
        "POST",
        f"/api/reminders/{item['key']}/reschedule",
        body={"etag": item["etag"], "schedule": "in 2h", "recurrence": {"kind": "daily"}},
    )
    assert response.status == 422
    current = await rig.reminders.get(42, item["key"])
    assert current.etag == item["etag"] and current.value.recurrence is None


async def test_recurring_controls_recheck_bot_rights_but_allow_cancelling_or_disabling_repeats(rig, monkeypatch):
    launch = await _community_access(rig, monkeypatch)
    response = await rig.api("POST", "/api/reminders", body=creation(launch=launch, recurrence={"kind": "daily"}))
    assert response.status == 201
    item = await response.json()
    await _community_access(rig, monkeypatch, bot_admin=False)
    response = await rig.api("POST", f"/api/reminders/{item['key']}/reschedule", body={"etag": item["etag"], "schedule": "in 2h"})
    assert response.status == 422
    assert (await rig.reminders.get(42, item["key"])).etag == item["etag"]
    response = await rig.api(
        "POST",
        f"/api/reminders/{item['key']}/reschedule",
        body={"etag": item["etag"], "schedule": "in 2h", "recurrence": None},
    )
    assert response.status == 200 and (await response.json())["recurrence"] is None

    await _community_access(rig, monkeypatch)
    item = await (await rig.api("POST", "/api/reminders", body=creation(launch=launch, recurrence={"kind": "daily"}))).json()
    await rig.reminders._terminal(await rig.reminders.get(42, item["key"]), "uncertain", failure="uncertain")
    held = await rig.reminders.get(42, item["key"])
    await _community_access(rig, monkeypatch, bot_admin=False)
    response = await rig.api("POST", f"/api/reminders/{item['key']}/retry", body={"etag": held.etag})
    assert response.status == 422 and (await rig.reminders.get(42, item["key"])).etag == held.etag
    response = await rig.api("POST", f"/api/reminders/{item['key']}/cancel", body={"etag": held.etag})
    assert response.status == 200 and (await response.json())["status"] == "cancelled"


async def test_recurring_permission_outage_is_retryable_without_committing_a_reminder(rig, monkeypatch):
    from aiogram.exceptions import TelegramNetworkError

    launch = await _community_access(rig, monkeypatch)
    original = rig.bot.session.make_request

    async def unavailable(bot, method, timeout=None):
        if isinstance(method, GetChatMember) and method.user_id == bot.id:
            raise TelegramNetworkError(method, "Synthetic outage")
        return await original(bot, method, timeout)

    monkeypatch.setattr(rig.bot.session, "make_request", unavailable)
    body = creation(launch=launch, recurrence={"kind": "daily"})
    response = await rig.api("POST", "/api/reminders", body=body)
    assert response.status == 503 and not rig.backend.records and not rig.backend.jobs
    monkeypatch.setattr(rig.bot.session, "make_request", original)
    response = await rig.api("POST", "/api/reminders", body=body)
    assert response.status == 201 and len(rig.backend.records) == len(rig.backend.jobs) == 1


async def test_chat_settings_preserve_unowned_fields_and_require_exact_revision(rig, monkeypatch):
    from msu_hub_bot.storage.application import APPLICATION, ChatPreferences

    launch = await _community_access(rig, monkeypatch)
    collection = rig.server.community.documents.settings
    tx = rig.reminders.store.transaction("settings", APPLICATION, operation_id=uuid4().hex)
    tx.expect_absent("chats", "-123")
    tx.put(collection, "-123", ChatPreferences(future_setting={"keep": True}))
    await tx.commit()
    path = f"/api/chats/-123/settings?launch={launch}"
    original = await (await rig.api("GET", path)).json()
    body = {"etag": original["etag"], "with_nsfw": True}
    response = await rig.api("PATCH", path, body=body)
    assert response.status == 200 and (await response.json())["values"]["with_nsfw"]
    assert (await rig.api("PATCH", path, body=body)).status == 409
    assert (await collection.get(APPLICATION, "-123")).value.model_extra == {"future_setting": {"keep": True}}


async def test_x_preview_setting_can_be_changed_independently(rig, monkeypatch):
    from msu_hub_bot.storage.application import APPLICATION, ChatPreferences

    launch = await _community_access(rig, monkeypatch)
    collection = rig.server.community.documents.settings
    tx = rig.reminders.store.transaction("settings", APPLICATION, operation_id=uuid4().hex)
    tx.expect_absent("chats", "-123")
    tx.put(collection, "-123", ChatPreferences(auto_video_links=False))
    await tx.commit()
    path = f"/api/chats/-123/settings?launch={launch}"
    initial = await (await rig.api("GET", path)).json()
    assert initial["values"]["auto_x_previews"] is False
    response = await rig.api("PATCH", path, body={"etag": initial["etag"], "auto_x_previews": True})
    assert response.status == 200
    updated = await response.json()
    assert updated["values"]["auto_x_previews"] is True
    assert updated["values"]["auto_video_links"] is False
    assert (await collection.get(APPLICATION, "-123")).value.auto_x_previews is True


async def test_mini_app_disabling_x_previews_updates_the_next_message_without_restart(rig, monkeypatch):
    from aiogram import Dispatcher
    from aiogram.types import Update

    from msu_hub_bot.storage.application import APPLICATION, ChatPreferences
    from msu_hub_bot.telegram.middlewares.settings import SettingsMiddleware
    from msu_hub_bot.telegram.middlewares.viewer import ViewerMiddleware

    launch = await _community_access(rig, monkeypatch)
    collection = rig.server.community.documents.settings
    tx = rig.reminders.store.transaction("settings", APPLICATION, operation_id=uuid4().hex)
    tx.expect_absent("chats", "-123")
    tx.put(collection, "-123", ChatPreferences())
    await tx.commit()

    async def load(chat):
        return (await collection.get(APPLICATION, str(chat.chat_id))).value.model_dump()

    database = SimpleNamespace(load_settings=AsyncMock(side_effect=load), patch_settings=AsyncMock())
    preferences = SettingsMiddleware(database)
    rig.server.settings_changed = AsyncMock(side_effect=preferences.invalidate)
    viewer = ViewerMiddleware(rig.bot, None, SimpleNamespace(run=AsyncMock()))
    viewer.links.handle_x_post = AsyncMock()
    dispatcher = Dispatcher(disable_fsm=True)
    dispatcher.message.outer_middleware(preferences)
    dispatcher.message.outer_middleware(viewer)
    url = "https://x.com/example/status/123"
    message = make_message(
        rig.bot,
        chat={"id": -123, "type": "supergroup", "title": "Synthetic chat"},
        text=url,
        entities=[{"type": "url", "offset": 0, "length": len(url)}],
    )
    await dispatcher.feed_update(rig.bot, Update(update_id=1, message=message))
    viewer.links.handle_x_post.assert_awaited_once()
    original = preferences.proxies[-123]
    assert original.auto_x_previews

    path = f"/api/chats/-123/settings?launch={launch}"
    current = await (await rig.api("GET", path)).json()
    body = {"etag": current["etag"], "auto_x_previews": False}
    rig.backend.lose_after_commit = 1
    response = await rig.api("PATCH", path, body=body)
    assert response.status == 200
    rig.server.settings_changed.assert_awaited_once_with(-123)
    assert (await rig.api("PATCH", path, body=body)).status == 409
    rig.server.settings_changed.assert_awaited_once_with(-123)

    await dispatcher.feed_update(rig.bot, Update(update_id=2, message=message))
    viewer.links.handle_x_post.assert_awaited_once()
    viewer.links.executor.run.assert_not_awaited()
    assert preferences.proxies[-123] is original
    assert not original.auto_x_previews
    assert database.load_settings.await_count == 2
    database.patch_settings.assert_not_awaited()


async def test_settings_cache_notification_requires_a_confirmed_commit(rig, monkeypatch):
    from msu_hub_bot.storage.application import APPLICATION, ApplicationDocuments, ChatPreferences

    launch = await _community_access(rig, monkeypatch)
    collection = rig.server.community.documents.settings
    tx = rig.reminders.store.transaction("settings", APPLICATION, operation_id=uuid4().hex)
    tx.expect_absent("chats", "-123")
    tx.put(collection, "-123", ChatPreferences())
    await tx.commit()
    rig.server.settings_changed = AsyncMock()
    path = f"/api/chats/-123/settings?launch={launch}"
    current = await (await rig.api("GET", path)).json()
    monkeypatch.setattr(ApplicationDocuments, "_commit", AsyncMock(side_effect=RepositoryUnavailable(RepositoryFailure.UNAVAILABLE)))
    response = await rig.api("PATCH", path, body={"etag": current["etag"], "auto_x_previews": False})
    assert response.status == 503
    rig.server.settings_changed.assert_not_awaited()
    assert (await collection.get(APPLICATION, "-123")).value.auto_x_previews


async def test_game_views_hide_live_answers_other_topics_and_unrelated_chat_players(rig, monkeypatch):
    from msu_hub_bot.games.models import Question, RoundState, Score
    from msu_hub_bot.storage.features import Scope

    launch = await _community_access(rig, monkeypatch, admin=False)
    scope = Scope("chat:-123")
    games = rig.server.community.games
    for kind in ("chess", "geoguess"):
        tx = rig.reminders.store.transaction(kind, scope, operation_id=uuid4().hex)
        for token, topic in (("synthetic-a", 17), ("synthetic-b", 18)):
            tx.expect_absent("rounds", token)
            tx.put(
                games.rounds[kind],
                token,
                RoundState(
                    token=token,
                    chat_id=-123,
                    thread_id=topic,
                    phase="active",
                    prepared_at=NOW,
                    question=Question(kind=kind, identity="hidden-answer-canary", choices=["hidden"] * 6, answer=3),
                ),
            )
        tx.expect_absent("scores", "2030-01-01:42")
        tx.put(games.scores[kind], "2030-01-01:42", Score(user_id=42, points=7, name="Synthetic"), parent="2030-01-01")
        await tx.commit()
        response = await rig.api("GET", f"/api/chats/-123/games?launch={launch}&kind={kind}")
        data = await response.json()
        assert response.status == 200 and len(data["history"]) == 1 and data["history"][0]["finished_at"] is None
        assert data["rankings"][0]["score"] == 7
        assert "hidden-answer-canary" not in await response.text() and "synthetic-b" not in await response.text()
    assert (await rig.api("GET", f"/api/chats/-999/games?launch={launch}&kind=chess")).status == 403
    assert (await rig.api("GET", f"/api/chats/-123/games?launch={launch}&kind=private")).status == 422


async def test_reaction_view_verifies_membership_and_bounds_window(rig, monkeypatch):
    from msu_hub_bot.storage.reactions import ReactionScoreboard

    launch = await _community_access(rig, monkeypatch, admin=False)
    board = ReactionScoreboard.model_validate(
        {
            "days": 7,
            "getters": [],
            "givers": [{"user_id": 42, "first_name": "Synthetic", "score": 3, "people": 2, "messages": 3}],
            "emoji": [],
            "posts": [],
            "summary": {
                key: 0
                for key in (
                    "points",
                    "reactions",
                    "givers",
                    "getters",
                    "messages",
                    "anonymous",
                    "paid",
                    "unattributed",
                    "channel_reactions",
                )
            },
        }
    )
    rig.server.database.reaction_scoreboard = AsyncMock(return_value=board)
    path = f"/api/chats/-123/reactions?launch={launch}&days=7"
    response = await rig.api("GET", path)
    data = await response.json()
    assert response.status == 200 and data["givers"][0]["score"] == 3
    rig.server.database.reaction_scoreboard.assert_awaited_once_with(-123, days=7, limit=10)
    assert (await rig.api("GET", f"/api/chats/-123/reactions?launch={launch}&days=365")).status == 422
    await _community_access(rig, monkeypatch, member=False)
    assert (await rig.api("GET", path)).status == 403
    assert rig.server.database.reaction_scoreboard.await_count == 1
