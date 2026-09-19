import { useState } from "react";
import { ApiClient, ApiError, errorMessage } from "../../platform/api";
import type { Reminder } from "../../platform/types";
import { Dialog } from "../../ui/Dialog";

export function ActionDialog({
  api,
  item,
  action,
  onClose,
  onUpdated,
}: {
  api: ApiClient;
  item: Reminder;
  action: "cancel" | "retry";
  onClose: () => void;
  onUpdated: (item: Reminder) => void;
}) {
  const [current, setCurrent] = useState(item);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const permitted =
    action === "cancel"
      ? ["pending", "failed", "uncertain"].includes(current.status)
      : ["failed", "uncertain"].includes(current.status);
  async function confirm() {
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      onUpdated(await api.action(current, action));
      onClose();
    } catch (caught) {
      setError(errorMessage(caught));
      if (
        caught instanceof ApiError &&
        (caught.status === 409 || caught.uncertain)
      ) {
        try {
          const latest = await api.get(item.key);
          setCurrent(latest);
          onUpdated(latest);
          const resolved =
            action === "cancel"
              ? latest.status === "cancelled"
              : ["pending", "sending", "delivered"].includes(latest.status);
          if (resolved) onClose();
          else
            setError(
              "Статус обновился. Проверьте напоминание и подтвердите действие ещё раз.",
            );
        } catch {
          setError(
            "Не удалось проверить результат. Обновите список перед повторной попыткой.",
          );
        }
      }
    } finally {
      setBusy(false);
    }
  }
  return (
    <Dialog
      title={
        action === "cancel"
          ? current.recurrence
            ? "Отменить все повторы?"
            : "Отменить напоминание?"
          : "Отправить ещё раз?"
      }
      onClose={onClose}
      busy={busy}
    >
      <p className="dialog-copy">
        {action === "cancel"
          ? current.recurrence
            ? "Все будущие повторы отменятся. Запись останется в истории."
            : "Бот больше не будет ждать его время. Запись останется в истории."
          : current.status === "uncertain"
            ? "Предыдущее сообщение могло прийти. Сначала проверьте чат: повтор может создать ещё одно сообщение."
            : "Бот попробует отправить напоминание сейчас."}
      </p>
      <blockquote className="action-preview">{current.text}</blockquote>
      {error && (
        <div className="notice error" role="alert">
          {error}
        </div>
      )}
      <div className="dialog-actions">
        <button className="button secondary" onClick={onClose} disabled={busy}>
          Назад
        </button>
        <button
          className={`button ${action === "cancel" ? "danger" : "primary"}`}
          disabled={busy || !permitted}
          onClick={() => void confirm()}
        >
          {busy
            ? "Подождите…"
            : action === "cancel"
              ? "Да, отменить"
              : "Да, отправить"}
        </button>
      </div>
    </Dialog>
  );
}
