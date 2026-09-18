import {
  act,
  render,
  renderHook,
  screen,
  waitFor,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { ApiClient } from "../src/platform/api";
import type { ReminderPage } from "../src/platform/types";
import { App } from "../src/app/App";
import { RemindersPage } from "../src/features/reminders/RemindersPage";
import { useReminders } from "../src/features/reminders/useReminders";
import { dateInput, tomorrowSchedule } from "../src/features/reminders/time";
import { reminder, session } from "./fixtures";

it("opens a useful guest screen without attempting API access", () => {
  render(<App />);
  expect(screen.getByRole("link", { name: "Открыть бота" })).toHaveAttribute(
    "href",
    "https://t.me/msu_hub_bot",
  );
  expect(fetch).not.toHaveBeenCalled();
});

it("loads more records explicitly and includes them in status filters", async () => {
  const api = new ApiClient("init");
  const list = vi
    .spyOn(api, "list")
    .mockResolvedValueOnce({
      items: [reminder()],
      next_cursor: "opaque-cursor",
    })
    .mockResolvedValueOnce({
      items: [
        reminder({ key: "history", text: "Уже сделано", status: "delivered" }),
      ],
      next_cursor: null,
    });
  const user = userEvent.setup();
  render(<RemindersPage api={api} session={session} />);
  expect(await screen.findByText("Полить цветы")).toBeVisible();
  expect(list).toHaveBeenCalledOnce();
  await user.click(screen.getByRole("button", { name: "Загрузить ещё" }));
  await waitFor(() => expect(list).toHaveBeenCalledTimes(2));
  expect(list.mock.calls[1]![0]).toBe("opaque-cursor");
  await user.click(screen.getByRole("button", { name: /История/ }));
  expect(await screen.findByText("Уже сделано")).toBeVisible();
  expect(screen.queryByText("Полить цветы")).not.toBeInTheDocument();
});

it("does not restore an older list snapshot over a completed mutation", async () => {
  let resolve: (page: ReminderPage) => void = () => {};
  const api = new ApiClient("init");
  vi.spyOn(api, "list").mockReturnValue(
    new Promise((done) => {
      resolve = done;
    }),
  );
  const { result } = renderHook(() => useReminders(api));
  const updated = reminder({ etag: "new", status: "cancelled" });
  act(() => result.current.update(updated));
  await act(async () => {
    resolve({ items: [reminder()], next_cursor: null });
  });
  expect(result.current.items).toEqual([updated]);
});

it("derives tomorrow from the selected zone rather than the device calendar", () => {
  const instant = new Date("2030-01-01T23:30:00Z");
  expect(tomorrowSchedule("Europe/Moscow", instant)).toBe(
    "at 2030-01-03 09:00",
  );
  expect(tomorrowSchedule("America/New_York", instant)).toBe(
    "at 2030-01-02 09:00",
  );
  expect(dateInput("2030-01-01T00:00:00Z", "Europe/Moscow")).toBe(
    "2030-01-01T03:00",
  );
});
