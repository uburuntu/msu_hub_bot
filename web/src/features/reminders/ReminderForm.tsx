import { useId, useRef, useState } from "react";
import type { FormEvent } from "react";
import { ApiClient, ApiError, errorMessage } from "../../platform/api";
import type {
  CreateReminder,
  Reminder,
  ReminderDraft,
  Session,
} from "../../platform/types";
import { Icon } from "../../ui/Icon";
import { dateInput, dueLabel, tomorrowSchedule, zoneLabel } from "./time";

export function ReminderForm({
  api,
  session,
  launch,
  item,
  onSaved,
  onBusy,
}: {
  api: ApiClient;
  session: Session;
  launch?: string;
  item?: Reminder;
  onSaved: (item: Reminder) => void;
  onBusy?: (busy: boolean) => void;
}) {
  const id = useId();
  const [text, setText] = useState(item?.text ?? "");
  const [timezone, setTimezone] = useState(
    item?.timezone ?? session.default_timezone,
  );
  const [when, setWhen] = useState(item ? "date" : "1h");
  const [date, setDate] = useState(
    item ? dateInput(item.due_at, item.timezone) : "",
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [uncertain, setUncertain] = useState(false);
  const [stale, setStale] = useState<Reminder | null>(null);
  const [base, setBase] = useState(item);
  const pending = useRef<CreateReminder | null>(null);
  const textarea = useRef<HTMLTextAreaElement>(null);
  const locked = busy || uncertain;
  const destination = item
    ? (item.destination_label ??
      (item.chat_id === session.context.chat_id &&
      item.thread_id === session.context.thread_id
        ? session.context.label
        : item.chat_id === session.user.id
          ? "Личные сообщения"
          : "Исходный чат"))
    : session.context.label;

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy || stale) return;
    setError("");
    let draft: ReminderDraft;
    try {
      const schedule =
        when === "date"
          ? `at ${date.replace("T", " ")}`
          : when === "tomorrow"
            ? tomorrowSchedule(timezone)
            : `in ${when}`;
      if (!text.trim()) {
        setError("Напишите, о чём напомнить.");
        textarea.current?.focus();
        return;
      }
      if (when === "date" && !date) {
        setError("Выберите дату и время.");
        return;
      }
      draft = { text: text.trim(), schedule, timezone };
    } catch {
      setError("Проверьте часовой пояс. Например, Europe/Moscow.");
      return;
    }
    setBusy(true);
    onBusy?.(true);
    try {
      let result: Reminder;
      if (base) {
        result = await api.reschedule(base, draft);
      } else {
        pending.current ??= {
          ...draft,
          request_id: crypto.randomUUID(),
          ...(launch ? { launch } : {}),
        };
        result = await api.create(pending.current);
      }
      pending.current = null;
      setUncertain(false);
      if (!base) {
        setText("");
        setDate("");
        setWhen("1h");
      }
      onSaved(result);
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 409 && base) {
        setError(
          "Напоминание уже изменилось. Ваш черновик сохранён — сравните его со свежей версией.",
        );
        try {
          setStale(await api.get(base.key));
        } catch {
          setError(
            "Не удалось проверить свежую версию. Черновик сохранён. Попробуйте ещё раз.",
          );
        }
      } else {
        setError(errorMessage(caught));
        if (!base && caught instanceof ApiError && caught.uncertain)
          setUncertain(true);
        else if (!base) pending.current = null;
      }
    } finally {
      setBusy(false);
      onBusy?.(false);
    }
  }

  return (
    <form
      className="reminder-form"
      onSubmit={(event) => void submit(event)}
      aria-busy={busy}
    >
      <label className="field-label" htmlFor={`${id}-text`}>
        О чём напомнить?
      </label>
      <div className="textarea-wrap">
        <textarea
          ref={textarea}
          id={`${id}-text`}
          value={text}
          onChange={(event) => setText(event.target.value)}
          placeholder="Забрать посылку. Полить цветы. Сделать паузу."
          rows={4}
          maxLength={3000}
          required
          readOnly={locked}
        />
        <span className="character-count">{text.length} / 3000</span>
      </div>
      <fieldset disabled={locked} className="schedule-fields">
        <legend className="field-label">Когда?</legend>
        <div className="quick-times">
          {[
            ["10m", "+10 мин"],
            ["1h", "+1 час"],
            ["tomorrow", "Завтра, 09:00"],
          ].map(([value, label]) => (
            <button
              key={value}
              type="button"
              className={`time-chip ${when === value ? "selected" : ""}`}
              aria-pressed={when === value}
              onClick={() => setWhen(value!)}
            >
              {label}
            </button>
          ))}
        </div>
        <button
          type="button"
          className={`date-choice ${when === "date" ? "selected" : ""}`}
          aria-pressed={when === "date"}
          onClick={() => setWhen("date")}
        >
          <Icon name="clock" size={17} />
          <span>Выбрать дату и время</span>
          <Icon name="arrow" size={15} />
        </button>
        {when === "date" && (
          <label className="datetime-label">
            Дата и время
            <input
              type="datetime-local"
              value={date}
              onChange={(event) => setDate(event.target.value)}
              required
            />
          </label>
        )}
        <label className="timezone-label" htmlFor={`${id}-zone`}>
          <Icon name="globe" size={15} />
          <span>Часовой пояс</span>
        </label>
        <input
          className="timezone-input"
          id={`${id}-zone`}
          value={timezone}
          onChange={(event) => setTimezone(event.target.value)}
          list={`${id}-zones`}
          autoComplete="off"
          spellCheck={false}
          required
          aria-describedby={`${id}-zone-hint`}
        />
        <datalist id={`${id}-zones`}>
          {[
            "Europe/Moscow",
            "Europe/London",
            "Europe/Berlin",
            "Asia/Yekaterinburg",
            "Asia/Almaty",
            "Asia/Tbilisi",
            "UTC",
          ].map((zone) => (
            <option key={zone} value={zone}>
              {zoneLabel(zone)}
            </option>
          ))}
        </datalist>
        <p className="field-hint" id={`${id}-zone-hint`}>
          Время — по выбранному часовому поясу.
        </p>
      </fieldset>
      <div className="destination">
        <span className="destination-icon">
          <Icon name="chat" size={16} />
        </span>
        <div>
          <span>Куда придёт</span>
          <strong>{destination}</strong>
        </div>
      </div>
      {error && (
        <div className="notice error" role="alert">
          {error}
        </div>
      )}
      {uncertain && (
        <div className="notice">
          <strong>Возможно, уже сохранено.</strong>
          <br />
          Проверим тот же запрос — без создания ещё одного напоминания. Черновик
          пока зафиксирован.
        </div>
      )}
      {stale && (
        <div className="stale-review">
          <span className="eyebrow">СВЕЖАЯ ВЕРСИЯ</span>
          <p>{stale.text}</p>
          <span>{dueLabel(stale.due_at, stale.timezone)}</span>
          <button
            type="button"
            className="button secondary"
            disabled={stale.status !== "pending"}
            onClick={() => {
              setBase(stale);
              setStale(null);
              setError("");
            }}
          >
            Оставить мой черновик для этой версии
          </button>
          {stale.status !== "pending" && (
            <p>
              Это напоминание больше не ожидает отправки. Его нельзя изменить.
            </p>
          )}
        </div>
      )}
      <button
        type="submit"
        className="button primary submit-button"
        disabled={busy || !!stale}
      >
        <Icon
          name={
            busy ? "refresh" : uncertain ? "refresh" : item ? "check" : "plus"
          }
        />
        {busy
          ? "Сохраняем…"
          : uncertain
            ? "Проверить и сохранить"
            : item
              ? "Сохранить изменения"
              : "Напомнить мне"}
      </button>
      {!item && (
        <p className="form-footnote">
          Бот напишет в Telegram. Можно выдохнуть.
        </p>
      )}
    </form>
  );
}
