import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { Workspace } from "../src/app/Workspace";
import { RepostForm } from "../src/features/community/RepostForm";
import { RepostsPage } from "../src/features/community/RepostsPage";
import { SettingsPage } from "../src/features/community/SettingsPage";
import { ApiClient, ApiError } from "../src/platform/api";
import { CommunityApi } from "../src/platform/community";
import type {
  Community,
  Repost,
  RepostPreview,
} from "../src/platform/communityTypes";
import { response, session } from "./fixtures";

const community: Community = {
  context: session.context,
  access: { member: true, admin: true, bot_admin: true },
  preferences: { timezone: "Europe/Moscow", etag: null },
  automatic_reposts: false,
};
const repost: Repost = {
  key: "source-one",
  etag: "version-one",
  owner_id: -123,
  source_url: "https://vk.com/club123",
  chat_id: -100,
  thread_id: 7,
  title: "Новости",
  with_reposts: false,
  with_header: true,
  include_keywords: [],
  exclude_keywords: [],
  last_post_id: 0,
  is_suspended: true,
  archived: false,
};
const preview: RepostPreview = {
  owner_id: -123,
  source_url: repost.source_url,
  available: true,
  reason: "Последние публикации",
  posts: [
    {
      id: 1,
      text: "Лекция",
      url: "https://vk.com/wall-123_1",
      is_repost: false,
      selected: true,
    },
  ],
  automatic_posting: false,
};

describe("community API", () => {
  it("binds chat reads to launch context and preserves credentials only in header", async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValue(response(community));
    const api = new CommunityApi("private-test", "signed context", fetcher);
    expect(await api.community()).toEqual(community);
    expect(fetcher.mock.calls[0]![0]).toBe(
      "/api/community?launch=signed+context",
    );
    expect(fetcher.mock.calls[0]![1]?.headers).toMatchObject({
      Authorization: "tma private-test",
    });
  });
  it("uses guarded PATCH without a posting enable field", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(response(repost));
    const api = new CommunityApi("init", "signed", fetcher);
    await api.saveRepost(repost, {
      title: "Новые новости",
      with_reposts: false,
      with_header: true,
      include_keywords: [],
      exclude_keywords: [],
      archived: true,
    });
    const [path, options] = fetcher.mock.calls[0]!;
    expect(path).toBe("/api/reposts/source-one?launch=signed");
    expect(options?.method).toBe("PATCH");
    expect(JSON.parse(String(options?.body))).toEqual({
      etag: "version-one",
      title: "Новые новости",
      with_reposts: false,
      with_header: true,
      include_keywords: [],
      exclude_keywords: [],
      archived: true,
    });
  });
  it.each([
    { ...repost, source_url: "javascript:alert(1)" },
    { ...repost, source_url: "https://vk.com.attacker.test/" },
    { ...repost, is_suspended: false },
    { ...repost, archived: "false" },
  ])("rejects unsafe or malformed repost records", async (item) => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValue(response({ items: [item], automatic_posting: false }));
    await expect(
      new CommunityApi("init", undefined, fetcher).reposts(),
    ).rejects.toMatchObject({ code: "protocol" });
  });
});

it("retries an uncertain create with the same frozen payload and UUID", async () => {
  const api = new CommunityApi("init");
  const create = vi
    .spyOn(api, "createRepost")
    .mockRejectedValueOnce(new ApiError("network", "Связь прервалась"))
    .mockResolvedValueOnce(repost);
  const user = userEvent.setup();
  render(<RepostForm api={api} destination="Друзья" onSaved={vi.fn()} />);
  await user.type(
    screen.getByLabelText("Источник VK"),
    "https://vk.com/club123",
  );
  await user.type(screen.getByLabelText("Название для себя"), "Новости");
  await user.click(screen.getByRole("button", { name: "Добавить на паузе" }));
  await screen.findByText(/Источник мог сохраниться/);
  expect(screen.getByLabelText("Источник VK")).toBeDisabled();
  await user.click(
    screen.getByRole("button", { name: "Проверить сохранение" }),
  );
  await waitFor(() => expect(create).toHaveBeenCalledTimes(2));
  expect(create.mock.calls[1]![0]).toEqual(create.mock.calls[0]![0]);
  expect(create.mock.calls[0]![0].request_id).toMatch(/^[a-f0-9-]{36}$/);
  expect(create.mock.calls[0]![0]).not.toHaveProperty("is_suspended");
});

it("invalidates preview after changing filters and never includes title in preview request", async () => {
  const api = new CommunityApi("init");
  const inspect = vi.spyOn(api, "previewRepost").mockResolvedValue(preview);
  const user = userEvent.setup();
  render(<RepostForm api={api} destination="Друзья" onSaved={vi.fn()} />);
  await user.type(
    screen.getByLabelText("Источник VK"),
    "https://vk.com/club123",
  );
  await user.click(
    screen.getByRole("button", { name: "Посмотреть публикации" }),
  );
  await screen.findByText("Лекция");
  expect(inspect.mock.calls[0]![0]).not.toHaveProperty("title");
  await user.click(screen.getByLabelText(/Включать репосты источника/));
  expect(screen.queryByText("Лекция")).not.toBeInTheDocument();
});

it("does not request or expose repost management for non-admin members", () => {
  const api = new CommunityApi("init");
  const list = vi.spyOn(api, "reposts");
  render(
    <RepostsPage
      api={api}
      community={{
        ...community,
        access: { ...community.access, admin: false },
      }}
    />,
  );
  expect(screen.queryByLabelText("Источник VK")).not.toBeInTheDocument();
  expect(list).not.toHaveBeenCalled();
  expect(screen.getByText("Для администраторов чата")).toBeVisible();
});

it("requires comparison before saving timezone over a stale revision", async () => {
  const api = new CommunityApi("init");
  const save = vi
    .spyOn(api, "savePreferences")
    .mockRejectedValueOnce(new ApiError("stale", "Настройки изменились", 409))
    .mockResolvedValueOnce({ timezone: "UTC", etag: "third" });
  vi.spyOn(api, "preferences").mockResolvedValue({
    timezone: "Europe/London",
    etag: "second",
  });
  const onTimezone = vi.fn();
  const user = userEvent.setup();
  render(
    <SettingsPage
      api={api}
      community={{
        ...community,
        access: { ...community.access, admin: false },
      }}
      onTimezone={onTimezone}
    />,
  );
  const field = screen.getByLabelText("Часовой пояс по умолчанию");
  await user.clear(field);
  await user.type(field, "UTC");
  await user.click(
    screen.getByRole("button", { name: "Сохранить часовой пояс" }),
  );
  expect(
    await screen.findByText(/Сейчас сохранено: Europe\/London/),
  ).toBeVisible();
  expect(field).toHaveValue("UTC");
  expect(
    screen.getByRole("button", { name: "Сохранить часовой пояс" }),
  ).toBeDisabled();
  await user.click(
    screen.getByRole("button", { name: "Сравнил, оставить мой выбор" }),
  );
  await user.click(
    screen.getByRole("button", { name: "Сохранить часовой пояс" }),
  );
  await waitFor(() => expect(onTimezone).toHaveBeenCalledWith("UTC"));
  expect(save.mock.calls[1]![0]).toEqual({ timezone: "UTC", etag: "second" });
});

it("preserves reminder and repost drafts while switching tools", async () => {
  const api = new ApiClient("init");
  vi.spyOn(api, "list").mockResolvedValue({ items: [], next_cursor: null });
  vi.spyOn(CommunityApi.prototype, "community").mockResolvedValue(community);
  vi.spyOn(CommunityApi.prototype, "reposts").mockResolvedValue({
    items: [],
    automatic_posting: false,
  });
  const user = userEvent.setup();
  const view = render(
    <Workspace api={api} credentials="init" session={session} />,
  );
  const desktopNavigation = within(
    view.container.querySelector(".sidebar nav") as HTMLElement,
  );
  await user.type(screen.getByLabelText("О чём напомнить?"), "Мой черновик");
  await user.click(desktopNavigation.getByRole("button", { name: "Репосты" }));
  await user.type(await screen.findByLabelText("Источник VK"), "club123");
  await user.click(
    desktopNavigation.getByRole("button", { name: "Напоминания" }),
  );
  expect(screen.getByLabelText("О чём напомнить?")).toHaveValue("Мой черновик");
  await user.click(desktopNavigation.getByRole("button", { name: "Репосты" }));
  expect(screen.getByLabelText("Источник VK")).toHaveValue("club123");
  fireEvent.click(screen.getByRole("button", { name: "Обновить источники" }));
  await waitFor(() =>
    expect(screen.getByLabelText("Источник VK")).toHaveValue("club123"),
  );
});
