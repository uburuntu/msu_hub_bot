import type {
  CreateReminder,
  Reminder,
  ReminderDraft,
  ReminderPage,
  Session,
} from "./types";

export class ApiError extends Error {
  constructor(
    public code: string,
    message: string,
    public status = 0,
  ) {
    super(message);
  }

  get uncertain(): boolean {
    return this.status === 0 || this.status >= 500;
  }
}

function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value))
    throw new ApiError(
      "protocol",
      "Сервер ответил неожиданно. Попробуйте ещё раз.",
      503,
    );
  return value as Record<string, unknown>;
}

function string(value: unknown): string {
  if (typeof value !== "string")
    throw new ApiError("protocol", "Не удалось прочитать ответ сервера.", 503);
  return value;
}

function integer(value: unknown): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value))
    throw new ApiError("protocol", "Не удалось прочитать ответ сервера.", 503);
  return value;
}

function stamp(value: unknown): string {
  const result = string(value);
  if (!Number.isFinite(Date.parse(result)))
    throw new ApiError("protocol", "Не удалось прочитать дату.", 503);
  return result;
}

function reminder(value: unknown): Reminder {
  const row = object(value);
  const status = string(row.status);
  if (
    ![
      "pending",
      "sending",
      "uncertain",
      "delivered",
      "cancelled",
      "failed",
    ].includes(status)
  )
    throw new ApiError("protocol", "Неизвестное состояние напоминания.", 503);
  const failure = row.failure;
  if (
    failure !== null &&
    !["rejected", "rate_limit", "uncertain"].includes(String(failure))
  )
    throw new ApiError(
      "protocol",
      "Не удалось прочитать состояние доставки.",
      503,
    );
  return {
    key: string(row.key),
    etag: string(row.etag),
    created_at: stamp(row.created_at),
    author_id: integer(row.author_id),
    author_name: string(row.author_name),
    chat_id: integer(row.chat_id),
    thread_id: row.thread_id === null ? null : integer(row.thread_id),
    text: string(row.text),
    due_at: stamp(row.due_at),
    timezone: string(row.timezone),
    status: status as Reminder["status"],
    attempts: integer(row.attempts),
    delivered_at: row.delivered_at === null ? null : stamp(row.delivered_at),
    failure: failure as Reminder["failure"],
    ...(row.destination_label === undefined
      ? {}
      : { destination_label: string(row.destination_label) }),
  };
}

export class ApiClient {
  constructor(
    private initData: string,
    private transport: typeof fetch = (...args) => fetch(...args),
  ) {}

  private async request<T>(
    path: string,
    decode: (data: unknown) => T,
    body?: unknown,
    signal?: AbortSignal,
  ): Promise<T> {
    if (!this.initData)
      throw new ApiError(
        "unauthorized",
        "Откройте приложение через Telegram.",
        401,
      );
    const controller = new AbortController();
    const abort = () => controller.abort();
    signal?.addEventListener("abort", abort, { once: true });
    if (signal?.aborted) controller.abort();
    const timer = window.setTimeout(abort, 20_000);
    try {
      const response = await this.transport(path, {
        method: body === undefined ? "GET" : "POST",
        headers: {
          Authorization: `tma ${this.initData}`,
          Accept: "application/json",
          ...(body === undefined ? {} : { "Content-Type": "application/json" }),
        },
        body: body === undefined ? undefined : JSON.stringify(body),
        credentials: "omit",
        cache: "no-store",
        redirect: "error",
        signal: controller.signal,
      });
      let data: unknown;
      try {
        data = await response.json();
      } catch {
        throw new ApiError(
          "protocol",
          "Не удалось прочитать ответ сервера.",
          503,
        );
      }
      if (!response.ok) {
        const error = object(object(data).error);
        throw new ApiError(
          string(error.code),
          string(error.message),
          response.status,
        );
      }
      return decode(data);
    } catch (error) {
      if (error instanceof ApiError) throw error;
      if (signal?.aborted) throw error;
      throw new ApiError(
        "network",
        "Связь прервалась. Проверьте интернет и попробуйте ещё раз.",
      );
    } finally {
      window.clearTimeout(timer);
      signal?.removeEventListener("abort", abort);
    }
  }

  session(launch?: string, signal?: AbortSignal): Promise<Session> {
    return this.request(
      `/api/session${launch ? `?launch=${encodeURIComponent(launch)}` : ""}`,
      (data) => {
        const row = object(data),
          user = object(row.user),
          context = object(row.context);
        return {
          user: { id: integer(user.id), name: string(user.name) },
          context: {
            chat_id: integer(context.chat_id),
            thread_id:
              context.thread_id === null ? null : integer(context.thread_id),
            label: string(context.label),
          },
          default_timezone: string(row.default_timezone),
          now: stamp(row.now),
        };
      },
      undefined,
      signal,
    );
  }

  list(after?: string, signal?: AbortSignal): Promise<ReminderPage> {
    return this.request(
      `/api/reminders?limit=50${after ? `&after=${encodeURIComponent(after)}` : ""}`,
      (data) => {
        const row = object(data);
        if (!Array.isArray(row.items))
          throw new ApiError(
            "protocol",
            "Не удалось прочитать напоминания.",
            503,
          );
        return {
          items: row.items.map(reminder),
          next_cursor:
            row.next_cursor === null ? null : string(row.next_cursor),
        };
      },
      undefined,
      signal,
    );
  }

  get(key: string): Promise<Reminder> {
    return this.request(`/api/reminders/${encodeURIComponent(key)}`, reminder);
  }
  create(body: CreateReminder): Promise<Reminder> {
    return this.request("/api/reminders", reminder, body);
  }
  reschedule(item: Reminder, draft: ReminderDraft): Promise<Reminder> {
    return this.request(
      `/api/reminders/${encodeURIComponent(item.key)}/reschedule`,
      reminder,
      { etag: item.etag, ...draft },
    );
  }
  action(item: Reminder, action: "cancel" | "retry"): Promise<Reminder> {
    return this.request(
      `/api/reminders/${encodeURIComponent(item.key)}/${action}`,
      reminder,
      { etag: item.etag },
    );
  }
}

export function errorMessage(error: unknown): string {
  return error instanceof ApiError
    ? error.message
    : "Что-то не получилось. Попробуйте ещё раз.";
}
