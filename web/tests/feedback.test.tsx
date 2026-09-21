import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Workspace } from "../src/app/Workspace";
import { FeedbackPage } from "../src/features/feedback/FeedbackPage";
import { ApiClient, ApiError } from "../src/platform/api";
import { CommunityApi } from "../src/platform/community";
import { FeedbackApi } from "../src/platform/feedback";
import type {
  FeedbackDetail,
  FeedbackReview,
  FeedbackSummary,
} from "../src/platform/feedbackTypes";
import { response, session } from "./fixtures";

const firstId = "a".repeat(16),
  secondId = "b".repeat(16);
function review(changes: Partial<FeedbackReview> = {}): FeedbackReview {
  return {
    report_id: firstId,
    author_id: 42,
    author_name: "Саша",
    summary: "Погода показывает вчерашний прогноз",
    kind: "bug",
    created_at: "2030-01-01T12:00:00Z",
    submitted_at: "2030-01-01T12:05:00Z",
    status: "new",
    reviewer_id: null,
    reviewed_at: null,
    etag: "review-one",
    note: "",
    ...changes,
  };
}
function summary(changes: Partial<FeedbackReview> = {}): FeedbackSummary {
  const { note: _note, ...item } = review(changes);
  return item;
}
function detail(changes: Partial<FeedbackReview> = {}): FeedbackDetail {
  const item = review(changes);
  return {
    report: {
      report_id: item.report_id,
      author_id: item.author_id,
      author_name: item.author_name,
      kind: item.kind,
      created_at: item.created_at,
      submitted_at: item.submitted_at,
      description: "Полный текст <img src=x onerror=alert(1)>\nВторая строка",
      rendered_text: "Точный снимок\nВыбранный автором текст <b>буквально</b>",
      context: {
        origin: {
          chat_id: -1001234567890,
          thread_id: 17,
          label: "Синтетический чат",
        },
        reply: {
          chat_id: -1001234567890,
          thread_id: 17,
          message_id: 99,
          author_id: 43,
          author_name: "Друг",
          sent_at: item.created_at,
          text: "Выбранное сообщение",
          media_kind: null,
          truncated: true,
        },
        recent_messages: [],
        recent_available: true,
        reply_available: true,
        diagnostics_since: null,
        diagnostics: [],
      },
      delivery: {
        status: "uncertain",
        attempts: 1,
        sending_at: item.submitted_at,
        sent_at: null,
        delivered_message_id: null,
        failure: "uncertain",
      },
    },
    review: item,
  };
}
function apiForPage(items: FeedbackSummary[] = [summary()]) {
  const api = new FeedbackApi("init");
  vi.spyOn(api, "feedbackList").mockResolvedValue({ items, next_cursor: null });
  vi.spyOn(api, "feedbackDetail").mockImplementation(async (id) =>
    detail({ report_id: id }),
  );
  return api;
}
afterEach(() => window.history.replaceState(null, "", "/"));

describe("feedback API", () => {
  it("defaults absent session capabilities to denied and decodes explicit owner access", async () => {
    const older = { ...session, capabilities: undefined };
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(response(older))
      .mockResolvedValueOnce(
        response({ ...session, capabilities: { feedback_review: true } }),
      );
    const api = new ApiClient("private-init", fetcher);
    expect((await api.session()).capabilities?.feedback_review).toBe(false);
    expect((await api.session()).capabilities?.feedback_review).toBe(true);
  });
  it("keeps credentials in headers and sends independent review etag without chat scope", async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(
        response({ items: [summary()], next_cursor: "opaque cursor" }),
      )
      .mockResolvedValueOnce(response(detail()))
      .mockResolvedValueOnce(
        response(review({ status: "done", etag: "review-two" })),
      );
    const api = new FeedbackApi("private-init", fetcher);
    expect(
      (
        await api.feedbackList(
          { status: "in_progress", kind: "idea" },
          "older cursor",
        )
      ).next_cursor,
    ).toBe("opaque cursor");
    const url = new URL(
      String(fetcher.mock.calls[0]![0]),
      "https://synthetic.invalid",
    );
    expect(Object.fromEntries(url.searchParams)).toEqual({
      limit: "30",
      status: "in_progress",
      kind: "idea",
      after: "older cursor",
    });
    expect(fetcher.mock.calls[0]![1]?.headers).toMatchObject({
      Authorization: "tma private-init",
    });
    expect(await api.feedbackDetail(firstId)).toEqual(detail());
    await api.saveFeedbackReview(firstId, {
      etag: "review-one",
      status: "done",
      note: "Проверено",
    });
    expect(fetcher.mock.calls[2]![0]).toBe(`/api/feedback/${firstId}/review`);
    expect(JSON.parse(String(fetcher.mock.calls[2]![1]?.body))).toEqual({
      etag: "review-one",
      status: "done",
      note: "Проверено",
    });
  });
  it("rejects unknown review states and preserves access denial", async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(
        response({
          items: [{ ...summary(), status: "sent" }],
          next_cursor: null,
        }),
      )
      .mockResolvedValueOnce(
        response(
          {
            error: { code: "access", message: "Доступ только сопровождающим" },
          },
          403,
        ),
      );
    const api = new FeedbackApi("init", fetcher);
    await expect(api.feedbackList({})).rejects.toMatchObject({
      code: "protocol",
    });
    await expect(api.feedbackDetail(firstId)).rejects.toMatchObject({
      status: 403,
    });
  });
});

it.each([undefined, { feedback_review: false }])(
  "hides review navigation and ignores deep links without capability %j",
  async (capabilities) => {
    window.history.replaceState(null, "", `/#feedback/${firstId}`);
    const api = new ApiClient("init");
    vi.spyOn(api, "list").mockResolvedValue({ items: [], next_cursor: null });
    vi.spyOn(CommunityApi.prototype, "community").mockRejectedValue(
      new ApiError("access", "Нет доступа к чату", 403),
    );
    const list = vi.spyOn(FeedbackApi.prototype, "feedbackList");
    const get = vi.spyOn(FeedbackApi.prototype, "feedbackDetail");
    render(
      <Workspace
        api={api}
        credentials="init"
        session={{ ...session, capabilities }}
      />,
    );
    await screen.findByLabelText("О чём напомнить?");
    expect(
      screen.queryByRole("button", { name: "Отзывы" }),
    ).not.toBeInTheDocument();
    expect(list).not.toHaveBeenCalled();
    expect(get).not.toHaveBeenCalled();
  },
);

it("opens an authorized report link even when community access fails", async () => {
  window.history.replaceState(null, "", `/#feedback/${firstId}`);
  const api = new ApiClient("init");
  vi.spyOn(api, "list").mockResolvedValue({ items: [], next_cursor: null });
  vi.spyOn(CommunityApi.prototype, "community").mockRejectedValue(
    new ApiError("access", "Нет доступа к чату", 403),
  );
  vi.spyOn(FeedbackApi.prototype, "feedbackList").mockResolvedValue({
    items: [],
    next_cursor: null,
  });
  const get = vi
    .spyOn(FeedbackApi.prototype, "feedbackDetail")
    .mockResolvedValue(detail({ status: "done" }));
  render(
    <Workspace
      api={api}
      credentials="init"
      session={{ ...session, capabilities: { feedback_review: true } }}
    />,
  );
  expect(await screen.findByLabelText("Приватная заметка")).toBeVisible();
  expect(get).toHaveBeenCalledWith(firstId, expect.any(AbortSignal));
  expect(screen.queryByText("Нет доступа к чату")).not.toBeInTheDocument();
  expect(screen.getByLabelText("Статус разбора")).toHaveValue("done");
});

it("paginates and resets the cursor when filters change", async () => {
  const api = apiForPage();
  const list = vi
    .spyOn(api, "feedbackList")
    .mockResolvedValueOnce({ items: [summary()], next_cursor: "next" })
    .mockResolvedValueOnce({
      items: [summary({ report_id: secondId, summary: "Новая идея" })],
      next_cursor: null,
    })
    .mockResolvedValue({ items: [], next_cursor: null });
  const user = userEvent.setup();
  render(<FeedbackPage api={api} />);
  await user.click(
    await screen.findByRole("button", { name: "Загрузить ещё" }),
  );
  expect(await screen.findByText("Новая идея")).toBeVisible();
  expect(list.mock.calls[1]!.slice(0, 2)).toEqual([{}, "next"]);
  await user.selectOptions(screen.getByLabelText("Статус"), "done");
  await screen.findByText("Пока тихо");
  expect(list.mock.calls[2]!.slice(0, 2)).toEqual([
    { status: "done" },
    undefined,
  ]);
  expect(screen.queryByText("Новая идея")).not.toBeInTheDocument();
  await user.selectOptions(screen.getByLabelText("Категория"), "idea");
  await waitFor(() =>
    expect(list).toHaveBeenLastCalledWith(
      { status: "done", kind: "idea" },
      undefined,
      expect.any(AbortSignal),
    ),
  );
});

it("ignores a late filter response and retries failed page loads", async () => {
  const api = apiForPage();
  let resolveOld!: (value: {
    items: FeedbackSummary[];
    next_cursor: null;
  }) => void;
  vi.spyOn(api, "feedbackList")
    .mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveOld = resolve;
        }),
    )
    .mockRejectedValueOnce(new ApiError("network", "Связь прервалась"))
    .mockResolvedValue({
      items: [summary({ status: "done" })],
      next_cursor: null,
    });
  const user = userEvent.setup();
  render(<FeedbackPage api={api} />);
  await user.selectOptions(screen.getByLabelText("Статус"), "done");
  await user.click(
    await screen.findByRole("button", { name: "Повторить загрузку" }),
  );
  await screen.findByText(summary().summary);
  await act(async () =>
    resolveOld({
      items: [summary({ report_id: secondId, summary: "Устаревший ответ" })],
      next_cursor: null,
    }),
  );
  expect(screen.queryByText("Устаревший ответ")).not.toBeInTheDocument();
});

it("shows the exact selected snapshot as text and saves review independently of delivery", async () => {
  const api = apiForPage();
  const save = vi.spyOn(api, "saveFeedbackReview").mockResolvedValue(
    review({
      status: "in_progress",
      note: "Проверить кэш",
      etag: "review-two",
    }),
  );
  const user = userEvent.setup();
  const view = render(<FeedbackPage api={api} />);
  await user.click(
    await screen.findByRole("button", { name: /Погода показывает/ }),
  );
  expect(await screen.findByText("Выбранное сообщение")).toBeVisible();
  expect(view.container.querySelector("img")).toBeNull();
  expect(
    screen.getByRole("link", { name: "Открыть сообщение ↗" }),
  ).toHaveAttribute("href", "https://t.me/c/1234567890/99");
  await user.click(screen.getByText("Точный снимок отзыва"));
  expect(
    screen.getByText("Точный снимок Выбранный автором текст <b>буквально</b>"),
  ).toBeVisible();
  await user.selectOptions(
    screen.getByLabelText("Статус разбора"),
    "in_progress",
  );
  await user.type(screen.getByLabelText("Приватная заметка"), "Проверить кэш");
  await user.click(screen.getByRole("button", { name: "Сохранить разбор" }));
  await screen.findByText("Статус и заметка сохранены.");
  expect(save).toHaveBeenCalledWith(firstId, {
    etag: "review-one",
    status: "in_progress",
    note: "Проверить кэш",
  });
  expect(
    screen.getByText("Уведомление в чате: Доставка не подтверждена"),
  ).toBeVisible();
});

it("keeps dirty notes across selection and requires explicit comparison after CAS conflict", async () => {
  const api = apiForPage([
    summary(),
    summary({ report_id: secondId, summary: "Вторая карточка" }),
  ]);
  const fresh = review({
    status: "done",
    note: "Изменено в другом окне",
    etag: "review-two",
  });
  const get = vi
    .spyOn(api, "feedbackDetail")
    .mockResolvedValueOnce(detail())
    .mockResolvedValueOnce(detail({ report_id: secondId }))
    .mockResolvedValueOnce(detail())
    .mockResolvedValueOnce({ ...detail(), review: fresh });
  const save = vi
    .spyOn(api, "saveFeedbackReview")
    .mockRejectedValueOnce(new ApiError("conflict", "Версия изменилась", 409))
    .mockResolvedValueOnce({
      ...fresh,
      status: "in_progress",
      note: "Мой план",
      etag: "review-three",
    });
  const user = userEvent.setup();
  render(<FeedbackPage api={api} />);
  await user.click(
    await screen.findByRole("button", { name: /Погода показывает/ }),
  );
  await user.type(
    await screen.findByLabelText("Приватная заметка"),
    "Мой план",
  );
  await user.selectOptions(
    screen.getByLabelText("Статус разбора"),
    "in_progress",
  );
  await user.click(screen.getByRole("button", { name: /Вторая карточка/ }));
  await waitFor(() =>
    expect(screen.getByLabelText("Приватная заметка")).toHaveValue(""),
  );
  await user.click(screen.getByRole("button", { name: /Погода показывает/ }));
  await waitFor(() =>
    expect(screen.getByLabelText("Приватная заметка")).toHaveValue("Мой план"),
  );
  await user.click(screen.getByRole("button", { name: "Сохранить разбор" }));
  await screen.findByText("Версия изменилась");
  expect(get).toHaveBeenCalledTimes(3);
  expect(screen.getByLabelText("Приватная заметка")).toHaveValue("Мой план");
  expect(
    screen.getByRole("button", { name: "Сохранить разбор" }),
  ).toBeDisabled();
  await user.click(
    screen.getByRole("button", { name: "Загрузить сохранённую версию" }),
  );
  await screen.findByText("Изменено в другом окне");
  expect(screen.getByLabelText("Приватная заметка")).toHaveValue("Мой план");
  await user.click(
    screen.getByRole("button", { name: "Сравнил, оставить мой вариант" }),
  );
  await user.click(screen.getByRole("button", { name: "Сохранить разбор" }));
  await screen.findByText("Статус и заметка сохранены.");
  expect(save.mock.calls[1]![1]).toEqual({
    etag: "review-two",
    status: "in_progress",
    note: "Мой план",
  });
});

it("shows detail access denial without a review editor", async () => {
  const api = apiForPage();
  vi.spyOn(api, "feedbackDetail").mockRejectedValue(
    new ApiError("access", "Доступ только сопровождающим", 403),
  );
  render(<FeedbackPage api={api} reportId={firstId} />);
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "Доступ только сопровождающим",
  );
  expect(screen.queryByLabelText("Приватная заметка")).not.toBeInTheDocument();
});

it("does not restore an old review status when a list refresh finishes after saving", async () => {
  const api = apiForPage();
  let resolveRefresh!: (value: {
    items: FeedbackSummary[];
    next_cursor: null;
  }) => void;
  vi.spyOn(api, "feedbackList")
    .mockResolvedValueOnce({ items: [summary()], next_cursor: null })
    .mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveRefresh = resolve;
        }),
    );
  vi.spyOn(api, "saveFeedbackReview").mockResolvedValue(
    review({ status: "done", etag: "review-two" }),
  );
  const user = userEvent.setup();
  render(<FeedbackPage api={api} />);
  await user.click(
    await screen.findByRole("button", { name: /Погода показывает/ }),
  );
  await user.selectOptions(
    await screen.findByLabelText("Статус разбора"),
    "done",
  );
  await user.click(screen.getByRole("button", { name: "Обновить отзывы" }));
  await screen.findByText("Загружаем отзывы…");
  await user.click(screen.getByRole("button", { name: "Сохранить разбор" }));
  await screen.findByText("Статус и заметка сохранены.");
  await act(async () =>
    resolveRefresh({ items: [summary()], next_cursor: null }),
  );
  expect(
    screen.getByRole("button", { name: /Погода показывает/ }),
  ).toHaveTextContent("Готово");
  expect(screen.getByLabelText("Статус разбора")).toHaveValue("done");
});

it.each([false, true])(
  "handles a Telegram launch query without trusting it for review access (%s)",
  async (allowed) => {
    window.history.replaceState(
      null,
      "",
      `/?feedback=${firstId}&launch=opaque#tgWebAppData=synthetic&tgWebAppVersion=9.0`,
    );
    const api = new ApiClient("init");
    vi.spyOn(api, "list").mockResolvedValue({ items: [], next_cursor: null });
    vi.spyOn(CommunityApi.prototype, "community").mockRejectedValue(
      new ApiError("access", "Нет доступа к чату", 403),
    );
    const list = vi
      .spyOn(FeedbackApi.prototype, "feedbackList")
      .mockResolvedValue({ items: [], next_cursor: null });
    const get = vi
      .spyOn(FeedbackApi.prototype, "feedbackDetail")
      .mockResolvedValue(detail());
    const user = userEvent.setup();
    render(
      <Workspace
        api={api}
        credentials="init"
        session={{ ...session, capabilities: { feedback_review: allowed } }}
        launch="opaque"
      />,
    );
    if (allowed) {
      expect(await screen.findByLabelText("Приватная заметка")).toBeVisible();
      expect(get).toHaveBeenCalledWith(firstId, expect.any(AbortSignal));
      await user.click(
        screen.getByRole("button", { name: "← К списку отзывов" }),
      );
      expect(new URLSearchParams(window.location.search).has("feedback")).toBe(
        false,
      );
      expect(new URLSearchParams(window.location.search).get("launch")).toBe(
        "opaque",
      );
    } else {
      expect(
        screen.queryByRole("button", { name: "Отзывы" }),
      ).not.toBeInTheDocument();
      expect(list).not.toHaveBeenCalled();
      expect(get).not.toHaveBeenCalled();
    }
  },
);

it("retries a failed first-page refresh with the same request despite an existing next cursor", async () => {
  const api = apiForPage();
  const list = vi
    .spyOn(api, "feedbackList")
    .mockResolvedValueOnce({ items: [summary()], next_cursor: "next-page" })
    .mockRejectedValueOnce(new ApiError("network", "Связь прервалась"))
    .mockResolvedValueOnce({
      items: [summary({ status: "done" })],
      next_cursor: null,
    });
  const user = userEvent.setup();
  render(<FeedbackPage api={api} />);
  await screen.findByRole("button", { name: /Погода показывает/ });
  await user.click(screen.getByRole("button", { name: "Обновить отзывы" }));
  await user.click(
    await screen.findByRole("button", { name: "Повторить загрузку" }),
  );
  await waitFor(() => expect(list).toHaveBeenCalledTimes(3));
  expect(list.mock.calls[2]!.slice(0, 2)).toEqual([{}, undefined]);
  expect(
    screen.getByRole("button", { name: /Погода показывает/ }),
  ).toHaveTextContent("Готово");
});

it("keeps a pending save bound to its report when the editor is reopened", async () => {
  const api = apiForPage([
    summary(),
    summary({ report_id: secondId, summary: "Вторая карточка" }),
  ]);
  let finish!: (value: FeedbackReview) => void;
  const save = vi.spyOn(api, "saveFeedbackReview").mockImplementationOnce(
    () =>
      new Promise((resolve) => {
        finish = resolve;
      }),
  );
  const user = userEvent.setup();
  render(<FeedbackPage api={api} />);
  await user.click(
    await screen.findByRole("button", { name: /Погода показывает/ }),
  );
  await user.type(
    await screen.findByLabelText("Приватная заметка"),
    "Первый план",
  );
  await user.click(screen.getByRole("button", { name: "Сохранить разбор" }));
  await user.click(screen.getByRole("button", { name: /Вторая карточка/ }));
  await waitFor(() =>
    expect(screen.getByLabelText("Приватная заметка")).toHaveValue(""),
  );
  expect(screen.getByLabelText("Приватная заметка")).not.toBeDisabled();
  await user.click(screen.getByRole("button", { name: /Погода показывает/ }));
  await waitFor(() =>
    expect(screen.getByLabelText("Приватная заметка")).toHaveValue(
      "Первый план",
    ),
  );
  expect(screen.getByLabelText("Приватная заметка")).toBeDisabled();
  expect(screen.getByLabelText("Статус разбора")).toBeDisabled();
  await act(async () =>
    finish(review({ note: "Первый план", etag: "review-two" })),
  );
  await waitFor(() =>
    expect(screen.getByLabelText("Приватная заметка")).not.toBeDisabled(),
  );
  await user.type(screen.getByLabelText("Приватная заметка"), " и дополнение");
  expect(screen.getByLabelText("Приватная заметка")).toHaveValue(
    "Первый план и дополнение",
  );
  expect(save).toHaveBeenCalledTimes(1);
});

it("applies a completed save to the current filter after switching filters while it was pending", async () => {
  const api = apiForPage();
  vi.spyOn(api, "feedbackList")
    .mockResolvedValueOnce({ items: [summary()], next_cursor: null })
    .mockResolvedValueOnce({
      items: [summary({ status: "done" })],
      next_cursor: null,
    });
  let finish!: (value: FeedbackReview) => void;
  vi.spyOn(api, "saveFeedbackReview").mockImplementationOnce(
    () =>
      new Promise((resolve) => {
        finish = resolve;
      }),
  );
  const user = userEvent.setup();
  render(<FeedbackPage api={api} />);
  await user.click(
    await screen.findByRole("button", { name: /Погода показывает/ }),
  );
  await user.selectOptions(
    await screen.findByLabelText("Статус разбора"),
    "done",
  );
  await user.click(screen.getByRole("button", { name: "Сохранить разбор" }));
  await user.selectOptions(screen.getByLabelText("Статус"), "done");
  await waitFor(() =>
    expect(
      screen.getByRole("button", { name: /Погода показывает/ }),
    ).toHaveTextContent("Готово"),
  );
  await act(async () => finish(review({ status: "done", etag: "review-two" })));
  expect(
    screen.getByRole("button", { name: /Погода показывает/ }),
  ).toHaveTextContent("Готово");
});
