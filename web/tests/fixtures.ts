import type { Reminder, Session } from "../src/platform/types";

export const session: Session = {
  user: { id: 42, name: "Саша" },
  context: { chat_id: -100, thread_id: 7, label: "Друзья · планы" },
  default_timezone: "Europe/Moscow",
  now: "2030-01-01T12:00:00Z",
};

export function reminder(changes: Partial<Reminder> = {}): Reminder {
  return {
    key: "reminder-one",
    etag: "version-one",
    created_at: "2030-01-01T12:00:00Z",
    author_id: 42,
    author_name: "Саша",
    chat_id: -100,
    thread_id: 7,
    text: "Полить цветы",
    due_at: "2030-01-01T13:00:00Z",
    timezone: "Europe/Moscow",
    status: "pending",
    attempts: 0,
    delivered_at: null,
    failure: null,
    destination_label: "Друзья · планы",
    ...changes,
  };
}

export function response(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}
