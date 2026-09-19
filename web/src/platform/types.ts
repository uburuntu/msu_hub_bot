export type Recurrence =
  | { kind: "daily" }
  | { kind: "weekly" }
  | { kind: "interval"; interval_minutes: number };

export type ReminderStatus =
  "pending" | "sending" | "uncertain" | "delivered" | "cancelled" | "failed";

export interface Reminder {
  key: string;
  etag: string;
  created_at: string;
  author_id: number;
  author_name: string;
  chat_id: number;
  thread_id: number | null;
  text: string;
  due_at: string;
  timezone: string;
  status: ReminderStatus;
  attempts: number;
  delivered_at: string | null;
  failure: "rejected" | "rate_limit" | "uncertain" | null;
  destination_label?: string;
  recurrence: Recurrence | null;
  occurrences: number;
  skipped_occurrences: number;
}

export interface Session {
  user: { id: number; name: string };
  context: { chat_id: number; thread_id: number | null; label: string };
  default_timezone: string;
  now: string;
}

export interface ReminderPage {
  items: Reminder[];
  next_cursor: string | null;
}

export interface ReminderDraft {
  text: string;
  schedule: string;
  timezone: string;
  recurrence?: Recurrence | null;
}

export interface CreateReminder extends ReminderDraft {
  request_id: string;
  launch?: string;
}
