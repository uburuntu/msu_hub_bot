"""Reviewer-only feedback HTTP contracts over a local Unix socket."""

import asyncio
import json
import tempfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import aiohttp
import pytest
from aiohttp import web

from msu_hub_bot.feedback import FeedbackContext, FeedbackMessage, FeedbackOrigin, FeedbackService
from msu_hub_bot.reminders import ReminderService
from msu_hub_bot.storage.features import FeatureStore, FeatureWorker
from msu_hub_bot.storage.features import jobs as feature_jobs
from msu_hub_bot.storage.features import store as feature_store
from msu_hub_bot.telemetry import Telemetry
from msu_hub_bot.web.links import WebAppLinks
from msu_hub_bot.web.server import WebServer
from quiz_helpers import FeatureFixture
from telegram_helpers import make_bot
from test_web import NOW, TOKEN, assert_headers, signed

OWNER = 42
PRIVATE = "PRIVATE_SELECTED_CONTEXT"
UNSELECTED = "UNSELECTED_CANDIDATE"
EXTRA = "UNREVIEWED_STORED_EXTRA"


@pytest.fixture
async def rig(monkeypatch):
    backend = FeatureFixture()
    backend.now = NOW

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return backend.now.replace(tzinfo=None) if tz is None else backend.now.astimezone(tz)

    monkeypatch.setattr(feature_jobs, "datetime", Clock)
    monkeypatch.setattr(feature_store, "datetime", Clock)
    bot = make_bot()
    store = FeatureStore(backend)
    worker = FeatureWorker(store)
    reminders = ReminderService(bot, store, worker)
    service = FeedbackService(bot, store, worker, destination_chat_id=-9876, reviewer_ids={OWNER})
    service.clock = lambda: backend.now
    links = WebAppLinks(TOKEN, "https://app.example")

    class Database:
        async def get_chat(self, chat_id):
            return SimpleNamespace(full_name="Synthetic chat")

    with tempfile.TemporaryDirectory(prefix="hub-feedback-web-", dir="/tmp") as directory:
        assets = Path(directory)
        (assets / "index.html").write_text("<html>Synthetic app</html>")
        server = WebServer(bot, reminders, Database(), links, Telemetry(), static_path=assets, feedback=service)
        server.clock = lambda: backend.now
        runner = web.AppRunner(server.application(), access_log=None)
        server.runner = runner
        await runner.setup()
        socket = str(assets / "http.sock")
        await web.UnixSite(runner, socket).start()
        try:
            async with aiohttp.ClientSession(connector=aiohttp.UnixConnector(path=socket)) as client:

                async def api(method, path, *, user_id=OWNER, body=None, data=None, headers=None):
                    request_headers = {
                        "Authorization": "tma " + signed(user={"id": user_id, "first_name": "Synthetic"}),
                        "Origin": links.url,
                        **(headers or {}),
                    }
                    return await client.request(method, "http://local" + path, json=body, data=data, headers=request_headers)

                yield SimpleNamespace(api=api, backend=backend, bot=bot, service=service, server=server, store=store)
        finally:
            await server.close()
            await bot.session.close()


async def submit(rig, *, author_id=7, message_id=10, kind="bug", description="Broken command <literal>"):
    candidates = FeedbackContext(
        origin=FeedbackOrigin(chat_id=-123, thread_id=17, label="Synthetic private chat"),
        reply=FeedbackMessage(chat_id=-123, thread_id=17, message_id=2, sent_at=NOW, author_name="Original author", text=PRIVATE),
        recent_messages=[FeedbackMessage(chat_id=-123, thread_id=17, message_id=1, sent_at=NOW, author_name="Other", text=UNSELECTED)],
    )
    draft = await rig.service.create(
        author_id=author_id,
        author_name="Reporter <&>",
        chat_id=-123,
        thread_id=17,
        source_message_id=message_id,
        description=description,
        kind=kind,
        candidates=candidates,
    )
    draft = await rig.service.bind(author_id, draft.key, chat_id=-123, message_id=100 + message_id, expected_etag=draft.etag)
    guard = {"ui_chat_id": -123, "ui_message_id": 100 + message_id}
    draft = await rig.service.preview(author_id, draft.key, expected_etag=draft.etag, **guard)
    return await rig.service.submit(author_id, draft.key, expected_etag=draft.etag, **guard)


async def test_session_capability_is_explicit_optional_and_independent_of_private_chat_access(rig):
    for user_id, allowed in [(OWNER, True), (7, False)]:
        response = await rig.api("GET", "/api/session", user_id=user_id)
        value = await response.json()
        assert response.status == 200 and value["context"]["chat_id"] == user_id
        assert value["capabilities"]["feedback_review"] is allowed
    rig.server.feedback.service = None
    response = await rig.api("GET", "/api/session")
    assert (await response.json())["capabilities"]["feedback_review"] is False
    rig.backend.calls.clear()
    assert (await rig.api("GET", "/api/feedback")).status == 403
    assert rig.backend.calls == []
    assert not rig.bot.session.methods


@pytest.mark.parametrize(
    "method,suffix,body",
    [("GET", "", None), ("GET", "/{key}", None), ("POST", "/{key}/review", {"etag": str(uuid4()), "status": "done", "note": "private"})],
)
async def test_reporter_or_chat_admin_cannot_enumerate_read_or_review_without_owner_allowlist(rig, method, suffix, body):
    report = await submit(rig)
    rig.backend.calls.clear()
    # Reporter7 has ordinary private-chat authority, but cannot review even their own report.
    response = await rig.api(method, "/api/feedback" + suffix.format(key=report.key), user_id=7, body=body)
    assert response.status == 403
    assert (await response.json())["error"]["code"] == "access"
    assert_headers(response)
    assert rig.backend.calls == [] and rig.bot.session.methods == []


async def test_denied_identity_cannot_probe_id_or_body_validity_and_auth_is_still_required(rig):
    for method, path, payload in [
        ("GET", "/api/feedback?status=secret", None),
        ("GET", "/api/feedback/invalid", None),
        ("POST", "/api/feedback/invalid/review", "not-json"),
    ]:
        response = await rig.api(method, path, user_id=7, data=payload)
        assert response.status == 403
    response = await rig.api("GET", "/api/feedback", headers={"Authorization": "tma forged"})
    assert response.status == 401
    assert rig.backend.calls == []


async def test_list_projects_bounded_summaries_with_filters_and_opaque_pagination_without_report_reads(rig):
    reports = [
        await submit(rig, author_id=100 + index, message_id=index + 1, kind=kind, description="Long description " * 50)
        for index, kind in enumerate(["bug", "idea", "bug"])
    ]
    _, review = await rig.service.review_get(OWNER, reports[0].key)
    await rig.service.review_update(OWNER, reports[0].key, expected_etag=review.etag, status="done", note="PRIVATE_REVIEW_NOTE")
    rig.backend.calls.clear()
    response = await rig.api("GET", "/api/feedback?limit=2")
    first = await response.json()
    assert response.status == 200 and len(first["items"]) == 2 and first["next_cursor"]
    assert len(rig.backend.calls) == 1 and rig.backend.calls[0][0] == "list"
    for item in first["items"]:
        assert len(item["summary"]) <= 240 and item["author_name"] == "Reporter <&>"
        assert not {"context", "description", "rendered_text", "note", "ui_digest"} & item.keys()
    response = await rig.api("GET", "/api/feedback?limit=2&after=" + first["next_cursor"])
    second = await response.json()
    assert len(second["items"]) == 1 and second["next_cursor"] is None
    assert len({item["report_id"] for item in first["items"] + second["items"]}) == 3
    response = await rig.api("GET", "/api/feedback?status=done&kind=bug&limit=50")
    assert [item["report_id"] for item in (await response.json())["items"]] == [reports[0].key]
    response = await rig.api("GET", "/api/feedback?status=new&kind=idea")
    assert [item["report_id"] for item in (await response.json())["items"]] == [reports[1].key]


async def test_detail_contains_exact_consented_text_context_and_separate_metadata_not_storage_extras(rig):
    report = await submit(rig)
    for item in rig.backend.records.values():
        if item["collection"] == "reports":
            item["payload"]["extra"] = EXTRA
            item["payload"]["context"]["extra"] = EXTRA
            item["payload"]["context"]["reply"]["extra"] = EXTRA
        elif item["collection"] == "review_index":
            item["payload"]["extra"] = EXTRA
    response = await rig.api("GET", f"/api/feedback/{report.key}")
    value = await response.json()
    assert response.status == 200
    assert value["report"]["rendered_text"] == report.value.rendered_text
    assert value["report"]["context"]["reply"]["text"] == PRIVATE
    assert value["report"]["context"]["recent_messages"] == []
    assert value["report"]["delivery"]["status"] == "queued" and value["review"]["status"] == "new"
    assert value["review"]["note"] == "" and value["review"]["etag"]
    serialized = json.dumps(value)
    assert (
        UNSELECTED not in serialized and EXTRA not in serialized and "ui_digest" not in serialized and "submission_etag" not in serialized
    )
    assert_headers(response)


async def test_review_cas_returns409_without_overwriting_note_or_frozen_report(rig):
    report = await submit(rig)
    detail = await (await rig.api("GET", f"/api/feedback/{report.key}")).json()
    path = f"/api/feedback/{report.key}/review"
    body = {"etag": detail["review"]["etag"], "status": "in_progress", "note": "Investigating <&>"}
    response = await rig.api("POST", path, body=body)
    saved = await response.json()
    assert response.status == 200 and saved["note"] == body["note"]
    assert saved["reviewer_id"] == OWNER and saved["reviewed_at"] and saved["etag"] != body["etag"]
    response = await rig.api("POST", path, body={**body, "status": "done", "note": "Unsaved browser draft"})
    assert response.status == 409 and (await response.json())["error"]["code"] == "conflict"
    current = await (await rig.api("GET", f"/api/feedback/{report.key}")).json()
    assert current["review"] == saved and current["report"] == detail["report"]


async def test_concurrent_review_changes_have_one_winner_and_keep_delivery_state_independent(rig):
    report = await submit(rig)
    _, review = await rig.service.review_get(OWNER, report.key)
    responses = await asyncio.gather(
        *(
            rig.api("POST", f"/api/feedback/{report.key}/review", body={"etag": review.etag, "status": "done", "note": f"Note {index}"})
            for index in range(2)
        )
    )
    assert sorted(response.status for response in responses) == [200, 409]
    current = await (await rig.api("GET", f"/api/feedback/{report.key}")).json()
    assert current["report"]["delivery"]["status"] == "queued"
    assert current["report"]["rendered_text"] == report.value.rendered_text


@pytest.mark.parametrize(
    "query",
    [
        "limit=0",
        "limit=51",
        "limit=1000000000000000000000000",
        "limit=-1",
        "limit=1.0",
        "limit=",
        "status=all",
        "status=",
        "kind=unknown",
        "after=invalid",
        "status=new&status=done",
        "unknown=1",
    ],
)
async def test_invalid_list_queries_are_rejected_before_storage(rig, query):
    response = await rig.api("GET", "/api/feedback?" + query)
    assert response.status == 422 and rig.backend.calls == []


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "sent"},
        {"note": None},
        {"note": "a" * 2001},
        {"note": 1},
        {"etag": "bad"},
        {"author_id": OWNER},
        {"note": None, "omit_note": True},
    ],
)
async def test_review_body_is_strict_and_rejects_oversize_or_unknown_fields_before_storage(rig, changes):
    body = {"etag": str(uuid4()), "status": "done", "note": "", **changes}
    if body.pop("omit_note", False):
        body.pop("note")
    response = await rig.api("POST", "/api/feedback/0123456789abcdef/review", body=body)
    assert response.status == 422 and rig.backend.calls == []


async def test_duplicate_json_and_total_body_cap_do_not_reach_storage(rig):
    path = "/api/feedback/0123456789abcdef/review"
    response = await rig.api("POST", path, data='{"status":"new","status":"done"}', headers={"Content-Type": "application/json"})
    assert response.status == 422
    response = await rig.api("POST", path, data=" " * (32 * 1024) + "{}", headers={"Content-Type": "application/json"})
    assert response.status == 413 and rig.backend.calls == []


async def test_unknown_report_is_safe404_and_outage_has_no_body_or_note_in_errors_or_logs(rig, caplog):
    response = await rig.api("GET", "/api/feedback/0123456789abcdef")
    assert response.status == 404 and (await response.json())["error"]["code"] == "not_found"
    report = await submit(rig)
    rig.backend.fail = True
    response = await rig.api("POST", f"/api/feedback/{report.key}/review", body={"etag": str(uuid4()), "status": "done", "note": PRIVATE})
    text = await response.text()
    assert response.status == 503 and PRIVATE not in text and PRIVATE not in caplog.text and TOKEN not in caplog.text
