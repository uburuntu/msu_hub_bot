"""Bounded feedback snapshots and recoverable private delivery state."""

from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, Field, field_validator, model_validator

from msu_hub_bot.storage.features import Payload

type FeedbackKind = Literal["bug", "idea", "other"]
type FeedbackStatus = Literal["queued", "sending", "sent", "uncertain", "failed"]
type FeedbackFailure = Literal["rejected", "rate_limit", "uncertain"]
type Identifier = Annotated[int, Field(strict=True, ge=-(2**63), lt=2**63)]
type PositiveIdentifier = Annotated[int, Field(strict=True, gt=0, lt=2**63)]


class FeedbackError(ValueError):
    """A fixed, safe explanation suitable for the draft's owner."""


class FeedbackOrigin(Payload):
    chat_id: Identifier
    thread_id: PositiveIdentifier | None = None
    label: str = Field(max_length=256)


class FeedbackMessage(Payload):
    chat_id: Identifier
    message_id: PositiveIdentifier
    sent_at: AwareDatetime
    author_name: str = Field(max_length=128)
    text: str = Field(max_length=2000)
    thread_id: PositiveIdentifier | None = None
    author_id: Identifier | None = None
    author_kind: Literal["user", "chat", "unknown"] = "unknown"
    media_kind: str | None = Field(default=None, max_length=40)
    truncated: bool = Field(default=False, strict=True)


class FeedbackDiagnostic(Payload):
    at: AwareDatetime
    handler: str = Field(max_length=128)
    outcome: Literal["completed", "ignored", "cancelled", "failed"]
    command: str | None = Field(default=None, max_length=64)
    message_id: PositiveIdentifier | None = None
    trace_id: str | None = Field(default=None, max_length=64)
    reason: str | None = Field(default=None, max_length=64)
    release: str | None = Field(default=None, max_length=80)


class SelectedFeedbackContext(Payload):
    origin: FeedbackOrigin | None = None
    reply: FeedbackMessage | None = None
    recent_messages: list[FeedbackMessage] = Field(default_factory=list, max_length=5)
    diagnostics: list[FeedbackDiagnostic] = Field(default_factory=list, max_length=5)
    recent_available: bool = Field(default=True, strict=True)
    reply_available: bool = Field(default=True, strict=True)
    diagnostics_since: AwareDatetime | None = None

    @model_validator(mode="after")
    def bounded_context(self) -> Self:
        if len(self.model_dump_json().encode("utf-8")) > 12 * 1024:
            raise ValueError("Feedback context exceeds its byte budget")
        return self


class FeedbackContext(SelectedFeedbackContext):
    origin: FeedbackOrigin

    @model_validator(mode="after")
    def context_scope(self) -> Self:
        messages = [*self.recent_messages, *([self.reply] if self.reply is not None else [])]
        if any((item.chat_id, item.thread_id) != (self.origin.chat_id, self.origin.thread_id) for item in messages):
            raise ValueError("Feedback candidates must stay inside their source chat and topic")
        return self


class FeedbackSelection(Payload):
    chat: bool = Field(default=True, strict=True)
    reply: bool = Field(default=True, strict=True)
    recent: bool = Field(default=False, strict=True)
    diagnostics: bool = Field(default=True, strict=True)


class FeedbackReport(Payload):
    report_id: str = Field(pattern=r"^[a-f0-9]{16}$")
    author_id: PositiveIdentifier
    author_name: str = Field(max_length=128)
    created_at: AwareDatetime
    kind: FeedbackKind = "bug"
    description: str = Field(min_length=1, max_length=2000)
    context: SelectedFeedbackContext
    destination_chat_id: Identifier
    destination_name: str = Field(default="Event Tracking", min_length=1, max_length=100)
    rendered_text: str = ""
    # Only a digest is needed to authorize a replay of the original submit button.
    # Origin identifiers otherwise occur only inside deliberately selected context.
    ui_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    submission_etag: str | None = None
    submitted_at: AwareDatetime | None = None
    status: FeedbackStatus = "queued"
    attempts: int = Field(default=0, strict=True, ge=0)
    sending_at: AwareDatetime | None = None
    sent_at: AwareDatetime | None = None
    delivered_message_id: PositiveIdentifier | None = None
    failure: FeedbackFailure | None = None

    @field_validator("rendered_text")
    @classmethod
    def bounded_rendering(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 24 * 1024:
            raise ValueError("Feedback rendering exceeds its byte budget")
        return value


class FeedbackDraft(Payload):
    author_id: PositiveIdentifier
    author_name: str = Field(max_length=128)
    chat_id: Identifier
    thread_id: PositiveIdentifier | None = None
    source_message_id: PositiveIdentifier
    description: str = Field(min_length=1, max_length=2000)
    created_at: AwareDatetime
    expires_at: AwareDatetime
    destination_chat_id: Identifier
    destination_name: str = Field(default="Event Tracking", min_length=1, max_length=100)
    context: FeedbackContext
    kind: FeedbackKind = "bug"
    selection: FeedbackSelection = Field(default_factory=FeedbackSelection)
    ui_chat_id: Identifier | None = None
    ui_message_id: PositiveIdentifier | None = None
    preview_digest: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def draft_contract(self) -> Self:
        if (self.ui_chat_id is None) != (self.ui_message_id is None):
            raise ValueError("Feedback UI binding requires both identifiers")
        if self.expires_at <= self.created_at:
            raise ValueError("Feedback draft requires a finite lifetime")
        if (self.context.origin.chat_id, self.context.origin.thread_id) != (self.chat_id, self.thread_id):
            raise ValueError("Feedback context must belong to the draft origin")
        return self


class FeedbackCreation(Payload):
    key: str = Field(pattern=r"^[a-f0-9]{16}$")
    chat_id: Identifier
    source_message_id: PositiveIdentifier
    expires_at: AwareDatetime


class FeedbackActivity(Payload):
    author_id: PositiveIdentifier
    active_draft: str | None = None
    active_until: AwareDatetime | None = None
    submissions: list[AwareDatetime] = Field(default_factory=list, max_length=5)
    creations: list[FeedbackCreation] = Field(default_factory=list, max_length=50)
