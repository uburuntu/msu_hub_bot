"""Reviewer-only HTTP projections of privately stored feedback reports."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING
from uuid import UUID

from aiohttp import web
from pydantic import BaseModel, ConfigDict, Field

from msu_hub_bot.feedback.models import (
    FeedbackAccessDenied,
    FeedbackKind,
    FeedbackMessage,
    FeedbackReport,
    FeedbackReview,
    FeedbackReviewStatus,
    SelectedFeedbackContext,
)
from msu_hub_bot.feedback.service import FeedbackService
from msu_hub_bot.storage.features import Record

if TYPE_CHECKING:
    from .server import WebServer


class ReviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    etag: UUID
    status: FeedbackReviewStatus
    note: str = Field(max_length=2000)


def _message(value: FeedbackMessage | None) -> dict[str, object] | None:
    if value is None:
        return None
    return value.model_dump(
        mode="json",
        include={
            "chat_id",
            "message_id",
            "sent_at",
            "author_name",
            "text",
            "thread_id",
            "author_id",
            "author_kind",
            "media_kind",
            "truncated",
        },
    )


def _context(value: SelectedFeedbackContext) -> dict[str, object]:
    return {
        "origin": value.origin.model_dump(mode="json", include={"chat_id", "thread_id", "label"}) if value.origin else None,
        "reply": _message(value.reply),
        "recent_messages": [_message(message) for message in value.recent_messages],
        "diagnostics": [
            item.model_dump(mode="json", include={"at", "handler", "outcome", "command", "message_id", "trace_id", "reason", "release"})
            for item in value.diagnostics
        ],
        "recent_available": value.recent_available,
        "reply_available": value.reply_available,
        "diagnostics_since": value.diagnostics_since.isoformat() if value.diagnostics_since else None,
    }


def _review(record: Record[FeedbackReview], *, detail: bool) -> dict[str, object]:
    fields = {
        "report_id",
        "author_id",
        "author_name",
        "summary",
        "kind",
        "created_at",
        "submitted_at",
        "status",
        "reviewer_id",
        "reviewed_at",
    }
    if detail:
        fields.add("note")
    return {**record.value.model_dump(mode="json", include=fields), "etag": record.etag}


def _report(record: Record[FeedbackReport]) -> dict[str, object]:
    value = record.value
    return {
        **value.model_dump(
            mode="json",
            include={"report_id", "author_id", "author_name", "created_at", "submitted_at", "kind", "description", "rendered_text"},
        ),
        "context": _context(value.context),
        "delivery": value.model_dump(
            mode="json", include={"status", "attempts", "sending_at", "sent_at", "delivered_message_id", "failure"}
        ),
    }


class FeedbackAPI:
    def __init__(self, server: WebServer, service: FeedbackService | None) -> None:
        self.server, self.service = server, service

    def register(self, app: web.Application) -> None:
        app.router.add_get("/api/feedback", self.list)
        app.router.add_get("/api/feedback/{report_id}", self.detail)
        app.router.add_post("/api/feedback/{report_id}/review", self.review)

    def allowed(self, user_id: int) -> bool:
        return self.service is not None and self.service.is_reviewer(user_id)

    def _authorize(self, request: web.Request) -> tuple[FeedbackService, int]:
        from .server import USER

        user_id = request[USER].id
        if self.service is None or not self.service.is_reviewer(user_id):
            raise FeedbackAccessDenied()
        return self.service, user_id

    @staticmethod
    def _key(request: web.Request) -> str:
        key = request.match_info["report_id"]
        if re.fullmatch(r"[a-f0-9]{16}", key) is None:
            raise ValueError("Invalid feedback identifier")
        return key

    async def list(self, request: web.Request) -> web.Response:
        service, reviewer_id = self._authorize(request)
        query = request.query
        if set(query) - {"status", "kind", "after", "limit"} or len(query) != len(set(query)):
            raise ValueError("Invalid feedback query")
        status: FeedbackReviewStatus | None = None
        if "status" in query:
            match query["status"]:
                case "new" | "in_progress" | "done" | "dismissed" as selected:
                    status = selected
                case _:
                    raise ValueError("Invalid feedback status")
        kind: FeedbackKind | None = None
        if "kind" in query:
            match query["kind"]:
                case "bug" | "idea" | "other" as selected_kind:
                    kind = selected_kind
                case _:
                    raise ValueError("Invalid feedback kind")
        after = query.get("after")
        if after is not None and re.fullmatch(r"[0-9]{16}:[a-f0-9]{16}", after) is None:
            raise ValueError("Invalid feedback cursor")
        raw_limit = query.get("limit", "20")
        if re.fullmatch(r"[1-9][0-9]?", raw_limit) is None or int(raw_limit) > 50:
            raise ValueError("Invalid feedback page size")
        limit = int(raw_limit)
        records = await service.review_list(reviewer_id, status=status, kind=kind, after=after, limit=limit)
        return web.json_response(
            {
                "items": [_review(record, detail=False) for record in records],
                "next_cursor": records[-1].key if len(records) == limit else None,
            }
        )

    async def detail(self, request: web.Request) -> web.Response:
        service, reviewer_id = self._authorize(request)
        if request.query:
            raise ValueError("Feedback detail does not accept query parameters")
        report, review = await service.review_get(reviewer_id, self._key(request))
        return web.json_response({"report": _report(report), "review": _review(review, detail=True)})

    async def review(self, request: web.Request) -> web.Response:
        service, reviewer_id = self._authorize(request)
        if request.query:
            raise ValueError("Feedback review does not accept query parameters")
        key = self._key(request)
        body = await self.server._body(request, ReviewInput)
        record = await service.review_update(reviewer_id, key, expected_etag=str(body.etag), status=body.status, note=body.note)
        return web.json_response(_review(record, detail=True))
