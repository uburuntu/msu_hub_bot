import { useCallback, useState } from "react";
import type { FormEvent } from "react";
import { ApiError, errorMessage } from "../../platform/api";
import type { FeedbackApi } from "../../platform/feedback";
import type {
  FeedbackContext,
  FeedbackMessage,
  FeedbackReview,
  ReviewStatus,
} from "../../platform/feedbackTypes";
import { LoadState } from "../community/ToolFrame";
import { useResource } from "../community/useResource";
import { feedbackTime, kindLabels, reviewLabels } from "./labels";

const deliveryLabels = {
  queued: "В очереди",
  sending: "Отправляется",
  sent: "Доставлено",
  uncertain: "Доставка не подтверждена",
  failed: "Не доставлено",
} as const;
export interface ReviewDraft {
  base: FeedbackReview;
  status: ReviewStatus;
  note: string;
  conflict: boolean;
}

export function FeedbackDetail({
  api,
  id,
  draft,
  pending,
  onPending,
  onDraft,
  onSaved,
}: {
  api: FeedbackApi;
  id: string;
  draft?: ReviewDraft;
  pending: boolean;
  onPending: (pending: boolean) => void;
  onDraft: (draft: ReviewDraft) => void;
  onSaved: (review: FeedbackReview) => void;
}) {
  const load = useCallback(
    (signal: AbortSignal) => api.feedbackDetail(id, signal),
    [api, id],
  );
  const state = useResource(load);
  const report = state.data?.report;
  return (
    <>
      <LoadState busy={state.busy} error={state.error} retry={state.refresh} />
      {report && state.data && (
        <>
          <article className="tool-card feedback-report">
            <div className="feedback-report-meta">
              <span className="feedback-kind">{kindLabels[report.kind]}</span>
              <code>#{report.report_id}</code>
            </div>
            <h2>{report.author_name}</h2>
            <p className="field-hint">
              Telegram ID {report.author_id} · Отправлено{" "}
              {feedbackTime(report.submitted_at)}
            </p>
            <p className="feedback-description" dir="auto">
              {report.description}
            </p>
            <div className="feedback-delivery">
              <strong>
                Уведомление в чате: {deliveryLabels[report.delivery.status]}
              </strong>
              <p>
                {report.delivery.status === "uncertain"
                  ? "Отзыв сохранён. Повторять уведомление автоматически не будем."
                  : "Состояние уведомления не меняет статус разбора отзыва."}
                {report.delivery.sent_at &&
                  ` ${feedbackTime(report.delivery.sent_at)}.`}
              </p>
            </div>
            <ContextView context={report.context} />
            <details className="feedback-exact">
              <summary>Точный снимок отзыва</summary>
              <p className="field-hint">
                Весь текст, который автор подтвердил перед отправкой.
              </p>
              <pre dir="auto">{report.rendered_text}</pre>
            </details>
          </article>
          <ReviewEditor
            api={api}
            initial={state.data.review}
            draft={draft}
            pending={pending}
            onPending={onPending}
            onDraft={onDraft}
            onSaved={onSaved}
          />
        </>
      )}
    </>
  );
}
function MessageView({ message }: { message: FeedbackMessage }) {
  const link =
    message.chat_id < -1_000_000_000_000
      ? `https://t.me/c/${-message.chat_id - 1_000_000_000_000}/${message.message_id}`
      : null;
  return (
    <div className="feedback-message">
      <strong>{message.author_name}</strong>
      <span className="field-hint">
        {message.author_id !== null && ` · ID ${message.author_id}`} ·{" "}
        {feedbackTime(message.sent_at)}
      </span>
      <p dir="auto">{message.text}</p>
      {message.media_kind && (
        <small>Медиа: {message.media_kind} (без вложения)</small>
      )}
      {message.truncated && (
        <small>Автору был показан и сохранён только этот фрагмент.</small>
      )}
      {link && (
        <a href={link} target="_blank" rel="noopener noreferrer">
          Открыть сообщение ↗
        </a>
      )}
    </div>
  );
}
function ContextView({ context }: { context: FeedbackContext }) {
  const present =
    context.origin ||
    context.reply ||
    context.recent_messages.length ||
    context.diagnostics_since;
  return (
    <section className="feedback-context">
      <h3>Выбранный контекст</h3>
      {!present && (
        <p className="field-hint">Автор не приложил дополнительный контекст.</p>
      )}
      {context.origin && (
        <div>
          <h4>Чат</h4>
          <p>{context.origin.label}</p>
          <p className="field-hint">
            ID {context.origin.chat_id}
            {context.origin.thread_id !== null &&
              ` · Тема ${context.origin.thread_id}`}
          </p>
        </div>
      )}
      {context.reply && (
        <div>
          <h4>Сообщение, на которое ответили</h4>
          <MessageView message={context.reply} />
        </div>
      )}
      {!context.reply_available && (
        <p className="field-hint">Сообщение для ответа было недоступно.</p>
      )}
      {context.recent_messages.length > 0 && (
        <div>
          <h4>Последние сообщения</h4>
          {context.recent_messages.map((message) => (
            <MessageView
              key={`${message.chat_id}/${message.message_id}`}
              message={message}
            />
          ))}
        </div>
      )}
      {!context.recent_available && (
        <p className="field-hint">История сообщений была недоступна.</p>
      )}
      {context.diagnostics_since && (
        <details>
          <summary>Диагностика команд · {context.diagnostics.length}</summary>
          <p className="field-hint">
            С {feedbackTime(context.diagnostics_since)}. Без аргументов команд и
            текстов ошибок.
          </p>
          {context.diagnostics.map((entry, index) => (
            <div className="feedback-diagnostic" key={index}>
              <strong>
                {entry.command ? `/${entry.command}` : entry.handler}
              </strong>
              <span>
                {entry.outcome} · {feedbackTime(entry.at)}
              </span>
              <dl>
                <dt>Обработчик</dt>
                <dd>{entry.handler}</dd>
                {entry.message_id !== null && (
                  <>
                    <dt>Сообщение</dt>
                    <dd>{entry.message_id}</dd>
                  </>
                )}
                {entry.reason && (
                  <>
                    <dt>Причина</dt>
                    <dd>{entry.reason}</dd>
                  </>
                )}
                {entry.trace_id && (
                  <>
                    <dt>Trace</dt>
                    <dd>{entry.trace_id}</dd>
                  </>
                )}
                {entry.release && (
                  <>
                    <dt>Версия</dt>
                    <dd>{entry.release}</dd>
                  </>
                )}
              </dl>
            </div>
          ))}
        </details>
      )}
    </section>
  );
}
function ReviewEditor({
  api,
  initial,
  draft,
  pending,
  onPending,
  onDraft,
  onSaved,
}: {
  api: FeedbackApi;
  initial: FeedbackReview;
  draft?: ReviewDraft;
  pending: boolean;
  onPending: (pending: boolean) => void;
  onDraft: (draft: ReviewDraft) => void;
  onSaved: (review: FeedbackReview) => void;
}) {
  const current = draft ?? {
    base: initial,
    status: initial.status,
    note: initial.note,
    conflict: false,
  };
  const [working, setBusy] = useState(false),
    [error, setError] = useState(""),
    [saved, setSaved] = useState(false);
  const [fresh, setFresh] = useState<FeedbackReview>();
  const [denied, setDenied] = useState(false);
  const busy = working || pending;
  const dirty =
    current.status !== current.base.status ||
    current.note !== current.base.note;
  const tooLong = Array.from(current.note).length > 2000;
  function edit(changes: Partial<ReviewDraft>) {
    onDraft({ ...current, ...changes });
    setSaved(false);
  }
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy || current.conflict || !dirty || tooLong || denied) return;
    setBusy(true);
    onPending(true);
    setError("");
    setSaved(false);
    try {
      const result = await api.saveFeedbackReview(initial.report_id, {
        etag: current.base.etag,
        status: current.status,
        note: current.note,
      });
      onDraft({
        base: result,
        status: result.status,
        note: result.note,
        conflict: false,
      });
      onSaved(result);
      setSaved(true);
    } catch (caught) {
      setError(errorMessage(caught));
      if (
        caught instanceof ApiError &&
        (caught.status === 409 || caught.uncertain)
      ) {
        onDraft({ ...current, conflict: true });
        setFresh(undefined);
      }
      if (caught instanceof ApiError && caught.status === 403) setDenied(true);
    } finally {
      setBusy(false);
      onPending(false);
    }
  }
  async function compare() {
    setBusy(true);
    setError("");
    try {
      setFresh((await api.feedbackDetail(initial.report_id)).review);
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="tool-card feedback-review">
      <h2>Разобрать отзыв</h2>
      <p className="field-hint">
        Статус и рабочая заметка приватны. Исходный отзыв остаётся неизменным.
      </p>
      <form
        className="tool-form"
        onSubmit={(event) => void submit(event)}
        aria-busy={busy}
      >
        <label>
          Статус разбора
          <select
            value={current.status}
            disabled={busy || denied}
            onChange={(event) =>
              edit({ status: event.target.value as ReviewStatus })
            }
          >
            {Object.entries(reviewLabels).map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <label>
          Приватная заметка
          <textarea
            value={current.note}
            rows={5}
            disabled={busy || denied}
            onChange={(event) => edit({ note: event.target.value })}
            placeholder="Что проверить, как воспроизвести, следующий шаг…"
          />
        </label>
        <p className="field-hint">
          {Array.from(current.note).length} / 2000 · Черновик остаётся только в
          этом окне до сохранения.
        </p>
        {tooLong && (
          <p className="notice error" role="alert">
            Сократите заметку до 2000 символов.
          </p>
        )}
        {error && (
          <p className="notice error" role="alert">
            {error}
          </p>
        )}
        {current.conflict && (
          <div className="stale-review">
            <p>
              Сохранение требует проверки: версия могла измениться. Ваши текст и
              статус остались в черновике.
            </p>
            {!fresh ? (
              <button
                type="button"
                className="text-button"
                disabled={busy}
                onClick={() => void compare()}
              >
                Загрузить сохранённую версию
              </button>
            ) : (
              <>
                <strong>Сейчас сохранено: {reviewLabels[fresh.status]}</strong>
                <p className="feedback-saved-note" dir="auto">
                  {fresh.note || "Без заметки"}
                </p>
                <div className="feedback-actions">
                  <button
                    type="button"
                    className="button secondary"
                    onClick={() => {
                      onDraft({ ...current, base: fresh, conflict: false });
                      setFresh(undefined);
                      setError("");
                    }}
                  >
                    Сравнил, оставить мой вариант
                  </button>
                  <button
                    type="button"
                    className="text-button"
                    onClick={() => {
                      onDraft({
                        base: fresh,
                        status: fresh.status,
                        note: fresh.note,
                        conflict: false,
                      });
                      setFresh(undefined);
                      setError("");
                    }}
                  >
                    Взять сохранённый вариант
                  </button>
                </div>
              </>
            )}
          </div>
        )}
        {saved && (
          <p className="success-notice" role="status">
            Статус и заметка сохранены.
          </p>
        )}
        <button
          className="button primary"
          disabled={busy || current.conflict || !dirty || tooLong || denied}
        >
          {busy ? "Сохраняем…" : "Сохранить разбор"}
        </button>
        {current.base.reviewed_at && (
          <p className="field-hint">
            Последнее изменение: {feedbackTime(current.base.reviewed_at)}
            {current.base.reviewer_id !== null &&
              ` · ID ${current.base.reviewer_id}`}
          </p>
        )}
      </form>
    </section>
  );
}
