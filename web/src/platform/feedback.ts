import { ApiClient, ApiError } from "./api";
import { array, boolean, integer, object, stamp, string } from "./decode";
import type {
  DeliveryStatus,
  FeedbackContext,
  FeedbackDetail,
  FeedbackFilter,
  FeedbackKind,
  FeedbackMessage,
  FeedbackPage,
  FeedbackReview,
  FeedbackSummary,
  ReviewStatus,
} from "./feedbackTypes";

function choice<T extends string>(value: unknown, choices: readonly T[]): T {
  const result = string(value);
  if (!choices.includes(result as T))
    throw new ApiError(
      "protocol",
      "Не удалось прочитать состояние отзыва.",
      503,
    );
  return result as T;
}
function nullable<T>(value: unknown, decode: (value: unknown) => T): T | null {
  return value === null ? null : decode(value);
}
function kind(value: unknown): FeedbackKind {
  return choice(value, ["bug", "idea", "other"]);
}
function summary(value: unknown): FeedbackSummary {
  const row = object(value);
  return {
    report_id: string(row.report_id),
    author_id: integer(row.author_id),
    author_name: string(row.author_name),
    summary: string(row.summary),
    kind: kind(row.kind),
    created_at: stamp(row.created_at),
    submitted_at: stamp(row.submitted_at),
    status: choice<ReviewStatus>(row.status, [
      "new",
      "in_progress",
      "done",
      "dismissed",
    ]),
    reviewer_id: nullable(row.reviewer_id, integer),
    reviewed_at: nullable(row.reviewed_at, stamp),
    etag: string(row.etag),
  };
}
function review(value: unknown): FeedbackReview {
  return { ...summary(value), note: string(object(value).note) };
}
function message(value: unknown): FeedbackMessage {
  const row = object(value);
  return {
    chat_id: integer(row.chat_id),
    message_id: integer(row.message_id),
    thread_id: nullable(row.thread_id, integer),
    author_id: nullable(row.author_id, integer),
    author_name: string(row.author_name),
    sent_at: stamp(row.sent_at),
    text: string(row.text),
    media_kind: nullable(row.media_kind, string),
    truncated: boolean(row.truncated),
  };
}
function context(value: unknown): FeedbackContext {
  const row = object(value);
  return {
    origin: nullable(row.origin, (value) => {
      const origin = object(value);
      return {
        chat_id: integer(origin.chat_id),
        thread_id: nullable(origin.thread_id, integer),
        label: string(origin.label),
      };
    }),
    reply: nullable(row.reply, message),
    recent_messages: array(row.recent_messages, message),
    recent_available: boolean(row.recent_available),
    reply_available: boolean(row.reply_available),
    diagnostics_since: nullable(row.diagnostics_since, stamp),
    diagnostics: array(row.diagnostics, (value) => {
      const entry = object(value);
      return {
        at: stamp(entry.at),
        handler: string(entry.handler),
        outcome: string(entry.outcome),
        command: nullable(entry.command, string),
        message_id: nullable(entry.message_id, integer),
        trace_id: nullable(entry.trace_id, string),
        reason: nullable(entry.reason, string),
        release: nullable(entry.release, string),
      };
    }),
  };
}
function detail(value: unknown): FeedbackDetail {
  const row = object(value),
    report = object(row.report),
    delivery = object(report.delivery);
  return {
    report: {
      report_id: string(report.report_id),
      author_id: integer(report.author_id),
      author_name: string(report.author_name),
      created_at: stamp(report.created_at),
      submitted_at: stamp(report.submitted_at),
      kind: kind(report.kind),
      description: string(report.description),
      rendered_text: string(report.rendered_text),
      context: context(report.context),
      delivery: {
        status: choice<DeliveryStatus>(delivery.status, [
          "queued",
          "sending",
          "sent",
          "uncertain",
          "failed",
        ]),
        attempts: integer(delivery.attempts),
        sending_at: nullable(delivery.sending_at, stamp),
        sent_at: nullable(delivery.sent_at, stamp),
        delivered_message_id: nullable(delivery.delivered_message_id, integer),
        failure: nullable(delivery.failure, string),
      },
    },
    review: review(row.review),
  };
}
export class FeedbackApi extends ApiClient {
  feedbackList(
    filters: FeedbackFilter,
    after?: string,
    signal?: AbortSignal,
  ): Promise<FeedbackPage> {
    const query = new URLSearchParams({ limit: "30" });
    if (filters.status) query.set("status", filters.status);
    if (filters.kind) query.set("kind", filters.kind);
    if (after) query.set("after", after);
    return this.request(
      `/api/feedback?${query}`,
      (value) => {
        const row = object(value);
        return {
          items: array(row.items, summary),
          next_cursor: nullable(row.next_cursor, string),
        };
      },
      undefined,
      signal,
    );
  }
  feedbackDetail(id: string, signal?: AbortSignal): Promise<FeedbackDetail> {
    return this.request(
      `/api/feedback/${encodeURIComponent(id)}`,
      detail,
      undefined,
      signal,
    );
  }
  saveFeedbackReview(
    id: string,
    body: { etag: string; status: ReviewStatus; note: string },
  ): Promise<FeedbackReview> {
    return this.request(
      `/api/feedback/${encodeURIComponent(id)}/review`,
      review,
      body,
    );
  }
}
