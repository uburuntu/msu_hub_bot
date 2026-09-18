import { useEffect, useRef, useState } from "react";
import type { ApiClient } from "../../platform/api";
import type { Reminder, Session } from "../../platform/types";
import { Icon } from "../../ui/Icon";
import { Dialog } from "../../ui/Dialog";
import { ActionDialog } from "./ActionDialog";
import { ReminderCard } from "./ReminderCard";
import { ReminderForm } from "./ReminderForm";
import { useReminders } from "./useReminders";

export function RemindersPage({
  api,
  session,
  launch,
}: {
  api: ApiClient;
  session: Session;
  launch?: string;
}) {
  const list = useReminders(api);
  const [filter, setFilter] = useState<"active" | "history">("active");
  const [editing, setEditing] = useState<Reminder | null>(null);
  const [editBusy, setEditBusy] = useState(false);
  const [action, setAction] = useState<{
    item: Reminder;
    kind: "cancel" | "retry";
  } | null>(null);
  const [notice, setNotice] = useState("");
  const clockOffset = useRef(Date.parse(session.now) - Date.now());
  const [now, setNow] = useState(Date.now() + clockOffset.current);
  const form = useRef<HTMLElement>(null);
  useEffect(() => {
    const timer = window.setInterval(
      () => setNow(Date.now() + clockOffset.current),
      30_000,
    );
    return () => window.clearInterval(timer);
  }, []);
  const active = list.items.filter(
    (item) => !["delivered", "cancelled"].includes(item.status),
  );
  const history = list.items.filter((item) =>
    ["delivered", "cancelled"].includes(item.status),
  );
  const shown = [...(filter === "active" ? active : history)].sort((a, b) =>
    filter === "active"
      ? Date.parse(a.due_at) - Date.parse(b.due_at)
      : Date.parse(b.due_at) - Date.parse(a.due_at),
  );

  return (
    <>
      <div className="page-heading">
        <div>
          <div className="eyebrow">
            <span className="eyebrow-dot" /> ОДНОЙ ЗАБОТОЙ МЕНЬШЕ
          </div>
          <h1>
            Напоминания<span className="heading-star">✳</span>
          </h1>
          <p>Для дел, идей и «не забыть бы».</p>
        </div>
        <button
          className="button primary new-reminder"
          onClick={() => {
            form.current?.scrollIntoView({
              behavior: "smooth",
              block: "start",
            });
            form.current
              ?.querySelector("textarea")
              ?.focus({ preventScroll: true });
          }}
        >
          <Icon name="plus" />
          Новое напоминание
        </button>
      </div>
      <div className="intro-strip">
        <span className="intro-icon">
          <Icon name="sparkle" size={21} />
        </span>
        <p>
          Освободите голову.<strong> Об остальном напомним в Telegram.</strong>
        </p>
        <span className="intro-doodle" aria-hidden="true">
          ↗
        </span>
      </div>
      {notice && (
        <div className="success-notice" role="status">
          <Icon name="check" size={18} />
          <span>{notice}</span>
          <button
            className="icon-button"
            aria-label="Скрыть уведомление"
            onClick={() => setNotice("")}
          >
            <Icon name="close" size={16} />
          </button>
        </div>
      )}
      <div className="reminders-layout">
        <section
          className="compose-card"
          ref={form}
          aria-labelledby="compose-heading"
        >
          <div className="compose-heading">
            <span className="compose-icon">
              <Icon name="plus" size={20} />
            </span>
            <div>
              <h2 id="compose-heading">На потом, без потерь</h2>
              <p>Запишите — и возвращайтесь к своим делам.</p>
            </div>
          </div>
          <ReminderForm
            api={api}
            session={session}
            launch={launch}
            onSaved={(item) => {
              list.update(item);
              setNotice("Готово. Напомним, когда придёт время.");
              setFilter("active");
            }}
          />
        </section>
        <section
          className="reminders-section"
          aria-labelledby="reminders-heading"
        >
          <div className="list-heading">
            <h2 id="reminders-heading">Ваши напоминания</h2>
            <button
              className={`icon-button ${list.busy ? "is-loading" : ""}`}
              disabled={list.busy}
              aria-label="Обновить напоминания"
              onClick={list.refresh}
            >
              <Icon name="refresh" size={18} />
            </button>
          </div>
          <div className="list-toolbar">
            <div
              className="filter-tabs"
              role="group"
              aria-label="Фильтр напоминаний"
            >
              <button
                aria-pressed={filter === "active"}
                className={filter === "active" ? "selected" : ""}
                onClick={() => setFilter("active")}
              >
                Предстоящие{" "}
                <span>
                  {active.length}
                  {list.cursor ? "+" : ""}
                </span>
              </button>
              <button
                aria-pressed={filter === "history"}
                className={filter === "history" ? "selected" : ""}
                onClick={() => setFilter("history")}
              >
                История{" "}
                <span>
                  {history.length}
                  {list.cursor ? "+" : ""}
                </span>
              </button>
            </div>
          </div>
          {list.error && (
            <div className="notice error" role="alert">
              {list.error}
              <button
                className="text-button"
                disabled={list.busy}
                onClick={list.refresh}
              >
                Попробовать ещё раз
              </button>
            </div>
          )}
          {!list.loaded && list.busy ? (
            <div
              className="skeleton-list"
              aria-label="Загружаем напоминания"
              role="status"
            >
              {[1, 2, 3].map((number) => (
                <div className="skeleton-card" key={number}>
                  <span />
                  <span />
                  <span />
                </div>
              ))}
            </div>
          ) : (
            <>
              {shown.length ? (
                <div className="reminder-list">
                  {shown.map((item) => (
                    <ReminderCard
                      key={item.key}
                      item={item}
                      session={session}
                      now={now}
                      onEdit={setEditing}
                      onAction={(entry, kind) =>
                        setAction({ item: entry, kind })
                      }
                    />
                  ))}
                </div>
              ) : (
                <div className="empty-state">
                  <span className="empty-orbit" aria-hidden="true">
                    <span className="empty-bell">
                      <Icon
                        name={filter === "active" ? "bell" : "check"}
                        size={34}
                      />
                    </span>
                    <span className="orbit-dot one" />
                    <span className="orbit-dot two" />
                    <span className="empty-star">✳</span>
                  </span>
                  <h3>
                    {list.cursor
                      ? "В этой части списка пока пусто"
                      : filter === "active"
                        ? "Всё под контролем"
                        : "История ещё впереди"}
                  </h3>
                  <p>
                    {list.cursor
                      ? "Загрузите следующую часть: там могут быть другие напоминания."
                      : filter === "active"
                        ? "Добавьте первое напоминание. Что-то важное, приятное или просто стакан воды."
                        : "Здесь появятся доставленные и отменённые напоминания."}
                  </p>
                  {filter === "active" && !list.cursor && (
                    <span className="empty-note">
                      Маленькие дела тоже считаются.
                    </span>
                  )}
                </div>
              )}
              {list.cursor && (
                <button
                  className="button secondary load-more"
                  disabled={list.busy}
                  onClick={list.more}
                >
                  {list.busy ? "Загружаем…" : "Загрузить ещё"}
                </button>
              )}
              {list.loaded && (
                <p className="list-footnote">
                  {list.cursor
                    ? `Загружено ${list.items.length}. Список продолжается.`
                    : "Вы видите все свои напоминания."}
                  {list.updatedAt &&
                    ` Обновлено в ${list.updatedAt.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" })}.`}
                </p>
              )}
            </>
          )}
        </section>
      </div>
      {editing && (
        <Dialog
          title="Изменить напоминание"
          onClose={() => setEditing(null)}
          busy={editBusy}
        >
          <ReminderForm
            api={api}
            session={session}
            item={editing}
            onBusy={setEditBusy}
            onSaved={(item) => {
              list.update(item);
              setEditing(null);
              setNotice("Изменения сохранены.");
            }}
          />
        </Dialog>
      )}
      {action && (
        <ActionDialog
          api={api}
          item={action.item}
          action={action.kind}
          onClose={() => setAction(null)}
          onUpdated={list.update}
        />
      )}
    </>
  );
}
