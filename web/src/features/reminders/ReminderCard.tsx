import type { Reminder, ReminderStatus, Session } from "../../platform/types";
import { Icon } from "../../ui/Icon";
import { dueLabel, zoneLabel } from "./time";

const labels: Record<ReminderStatus, string> = {
  pending: "Запланировано",
  sending: "Отправляется",
  uncertain: "Проверить доставку",
  delivered: "Доставлено",
  cancelled: "Отменено",
  failed: "Не доставлено",
};
export function ReminderCard({
  item,
  session,
  now,
  onEdit,
  onAction,
}: {
  item: Reminder;
  session: Session;
  now: number;
  onEdit: (item: Reminder) => void;
  onAction: (item: Reminder, action: "cancel" | "retry") => void;
}) {
  const overdue = item.status === "pending" && Date.parse(item.due_at) < now;
  const destination =
    item.destination_label ??
    (item.chat_id === session.context.chat_id &&
    item.thread_id === session.context.thread_id
      ? session.context.label
      : item.chat_id === session.user.id
        ? "Личные сообщения"
        : "Другой чат");
  return (
    <article className={`reminder-card status-${item.status}`}>
      <div className="reminder-card-head">
        <div className="due">
          <Icon
            name={item.status === "delivered" ? "check" : "clock"}
            size={17}
          />
          <time dateTime={item.due_at}>
            {dueLabel(item.due_at, item.timezone)}
          </time>
        </div>
        <span className={`status-badge ${overdue ? "overdue" : item.status}`}>
          {overdue ? "Время наступило" : labels[item.status]}
        </span>
      </div>
      <p className="reminder-text">{item.text}</p>
      {item.recurrence && (
        <p className="recurrence-note">
          <Icon name="refresh" size={14} />{" "}
          {item.recurrence.kind === "daily"
            ? "Каждый день"
            : item.recurrence.kind === "weekly"
              ? "Каждую неделю"
              : `Каждые ${item.recurrence.interval_minutes} мин`}
          {item.occurrences > 0 ? ` · Доставлено: ${item.occurrences}` : ""}
          {item.skipped_occurrences > 0
            ? ` · Пропуски объединены: ${item.skipped_occurrences}`
            : ""}
        </p>
      )}
      {item.status === "uncertain" && (
        <p className="delivery-note">
          Telegram не подтвердил доставку. Сообщение могло прийти — проверьте
          чат перед повтором.
          {item.recurrence
            ? " Всё расписание приостановлено до вашей проверки."
            : ""}
        </p>
      )}
      {item.status === "failed" && (
        <p className="delivery-note">
          Не удалось отправить. Проверьте, что бот доступен в чате, и попробуйте
          снова.
        </p>
      )}
      <div className="reminder-card-foot">
        <div className="reminder-meta">
          <span>
            <Icon name="chat" size={14} />
            {destination}
          </span>
          <span className="zone-meta">{zoneLabel(item.timezone)}</span>
        </div>
        <div className="card-actions">
          {item.status === "pending" && (
            <button
              className="icon-button"
              aria-label={`Изменить: ${item.text.slice(0, 50)}`}
              onClick={() => onEdit(item)}
            >
              <Icon name="edit" size={17} />
            </button>
          )}
          {["failed", "uncertain"].includes(item.status) && (
            <button
              className="small-button"
              onClick={() => onAction(item, "retry")}
            >
              <Icon name="refresh" size={15} />
              Повторить
            </button>
          )}
          {["pending", "failed", "uncertain"].includes(item.status) && (
            <button
              className="icon-button cancel-action"
              aria-label={`Отменить: ${item.text.slice(0, 50)}`}
              onClick={() => onAction(item, "cancel")}
            >
              <Icon name="close" size={17} />
            </button>
          )}
        </div>
      </div>
    </article>
  );
}
