import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { ReminderForm } from "../src/features/reminders/ReminderForm";
import { ApiClient, ApiError } from "../src/platform/api";
import { reminder, session } from "./fixtures";

it("retries an uncertain create with the exact frozen UUID and payload", async () => {
  const api = new ApiClient("init");
  const create = vi
    .spyOn(api, "create")
    .mockRejectedValueOnce(new ApiError("network", "Связь прервалась"))
    .mockResolvedValueOnce(reminder());
  const saved = vi.fn();
  const user = userEvent.setup();
  render(
    <ReminderForm
      api={api}
      session={session}
      launch="signed-context"
      onSaved={saved}
    />,
  );
  await user.type(screen.getByLabelText("О чём напомнить?"), "Полить цветы");
  await user.click(screen.getByRole("button", { name: "Напомнить мне" }));
  await screen.findByText("Возможно, уже сохранено.");
  expect(screen.getByLabelText("О чём напомнить?")).toHaveAttribute("readonly");
  expect(create.mock.calls[0]![0]).toMatchObject({
    text: "Полить цветы",
    schedule: "in 1h",
    timezone: "Europe/Moscow",
    launch: "signed-context",
  });
  await user.click(
    screen.getByRole("button", { name: "Проверить и сохранить" }),
  );
  await waitFor(() => expect(saved).toHaveBeenCalledOnce());
  expect(create.mock.calls[0]![0]).toEqual(create.mock.calls[1]![0]);
  expect(create.mock.calls[0]![0].request_id).toMatch(/^[a-f0-9-]{36}$/);
  expect(screen.getByLabelText("О чём напомнить?")).toHaveValue("");
});

it("preserves editable draft after a validation error", async () => {
  const api = new ApiClient("init");
  vi.spyOn(api, "create").mockRejectedValue(
    new ApiError("invalid", "Проверьте время", 422),
  );
  const user = userEvent.setup();
  render(<ReminderForm api={api} session={session} onSaved={vi.fn()} />);
  await user.type(screen.getByLabelText("О чём напомнить?"), "Позвонить другу");
  await user.click(screen.getByRole("button", { name: "Напомнить мне" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Проверьте время");
  expect(screen.getByLabelText("О чём напомнить?")).toHaveValue(
    "Позвонить другу",
  );
  expect(screen.getByLabelText("О чём напомнить?")).not.toHaveAttribute(
    "readonly",
  );
});

it("requires explicit comparison before applying a stale edit to a fresh revision", async () => {
  const api = new ApiClient("init");
  const newer = reminder({
    etag: "version-two",
    text: "Другой сохранённый текст",
  });
  const edit = vi
    .spyOn(api, "reschedule")
    .mockRejectedValueOnce(new ApiError("stale", "Changed", 409))
    .mockResolvedValueOnce(reminder({ etag: "version-three" }));
  vi.spyOn(api, "get").mockResolvedValue(newer);
  const user = userEvent.setup();
  render(
    <ReminderForm
      api={api}
      session={session}
      item={reminder()}
      onSaved={vi.fn()}
    />,
  );
  const text = screen.getByLabelText("О чём напомнить?");
  await user.clear(text);
  await user.type(text, "Мой черновик");
  await user.click(screen.getByRole("button", { name: "Сохранить изменения" }));
  expect(await screen.findByText("Другой сохранённый текст")).toBeVisible();
  expect(text).toHaveValue("Мой черновик");
  expect(
    screen.getByRole("button", { name: "Сохранить изменения" }),
  ).toBeDisabled();
  await user.click(
    screen.getByRole("button", {
      name: "Оставить мой черновик для этой версии",
    }),
  );
  await user.click(screen.getByRole("button", { name: "Сохранить изменения" }));
  await waitFor(() => expect(edit).toHaveBeenCalledTimes(2));
  expect(edit.mock.calls[1]![0].etag).toBe("version-two");
  expect(edit.mock.calls[1]![1].text).toBe("Мой черновик");
});

it("retains partially typed date and text when unrelated parent content refreshes", async () => {
  const api = new ApiClient("init");
  const view = render(
    <ReminderForm api={api} session={session} onSaved={vi.fn()} />,
  );
  const user = userEvent.setup();
  await user.type(
    screen.getByLabelText("О чём напомнить?"),
    "Длинный черновик",
  );
  await user.click(
    screen.getByRole("button", { name: "Выбрать дату и время" }),
  );
  fireEvent.change(screen.getByLabelText("Дата и время"), {
    target: { value: "2030-05-12T15:45" },
  });
  view.rerender(
    <ReminderForm
      api={api}
      session={{ ...session, now: "2030-01-01T12:01:00Z" }}
      onSaved={vi.fn()}
    />,
  );
  expect(screen.getByLabelText("О чём напомнить?")).toHaveValue(
    "Длинный черновик",
  );
  expect(screen.getByLabelText("Дата и время")).toHaveValue("2030-05-12T15:45");
});
