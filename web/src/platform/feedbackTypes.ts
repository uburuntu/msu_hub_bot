export type FeedbackKind = "bug" | "idea" | "other";
export type ReviewStatus = "new" | "in_progress" | "done" | "dismissed";
export type DeliveryStatus =
  "queued" | "sending" | "sent" | "uncertain" | "failed";

export interface FeedbackSummary {
  report_id: string;
  author_id: number;
  author_name: string;
  summary: string;
  kind: FeedbackKind;
  created_at: string;
  submitted_at: string;
  status: ReviewStatus;
  reviewer_id: number | null;
  reviewed_at: string | null;
  etag: string;
}
export interface FeedbackReview extends FeedbackSummary {
  note: string;
}
export interface FeedbackMessage {
  chat_id: number;
  message_id: number;
  thread_id: number | null;
  author_id: number | null;
  author_name: string;
  sent_at: string;
  text: string;
  media_kind: string | null;
  truncated: boolean;
}
export interface FeedbackContext {
  origin: { chat_id: number; thread_id: number | null; label: string } | null;
  reply: FeedbackMessage | null;
  recent_messages: FeedbackMessage[];
  recent_available: boolean;
  reply_available: boolean;
  diagnostics_since: string | null;
  diagnostics: {
    at: string;
    handler: string;
    outcome: string;
    command: string | null;
    message_id: number | null;
    trace_id: string | null;
    reason: string | null;
    release: string | null;
  }[];
}
export interface FeedbackDetail {
  report: {
    report_id: string;
    author_id: number;
    author_name: string;
    created_at: string;
    submitted_at: string;
    kind: FeedbackKind;
    description: string;
    rendered_text: string;
    context: FeedbackContext;
    delivery: {
      status: DeliveryStatus;
      attempts: number;
      sending_at: string | null;
      sent_at: string | null;
      delivered_message_id: number | null;
      failure: string | null;
    };
  };
  review: FeedbackReview;
}
export interface FeedbackPage {
  items: FeedbackSummary[];
  next_cursor: string | null;
}
export interface FeedbackFilter {
  status?: ReviewStatus;
  kind?: FeedbackKind;
}
