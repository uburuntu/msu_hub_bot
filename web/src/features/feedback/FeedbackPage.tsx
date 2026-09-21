import { useCallback, useEffect, useRef, useState } from "react";
import { errorMessage } from "../../platform/api";
import type { FeedbackApi } from "../../platform/feedback";
import type {
  FeedbackKind,
  FeedbackReview,
  FeedbackSummary,
  ReviewStatus,
} from "../../platform/feedbackTypes";
import { Icon } from "../../ui/Icon";
import { ToolFrame } from "../community/ToolFrame";
import { FeedbackDetail, type ReviewDraft } from "./FeedbackDetail";
import { feedbackTime, kindLabels, reviewLabels } from "./labels";

export function FeedbackPage({
  api,
  reportId,
}: {
  api: FeedbackApi;
  reportId?: string;
}) {
  const [status, setStatus] = useState<ReviewStatus | "">("");
  const [kind, setKind] = useState<FeedbackKind | "">("");
  const currentFilters = useRef({ status, kind });
  useEffect(() => {
    currentFilters.current = { status, kind };
  }, [status, kind]);
  const [items, setItems] = useState<FeedbackSummary[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  const [selected, setSelected] = useState(reportId);
  const [drafts, setDrafts] = useState<Record<string, ReviewDraft>>({});
  const [pending, setPending] = useState<Record<string, boolean>>({});
  const active = useRef<AbortController | null>(null);
  const failedAfter = useRef<string | undefined>(undefined);
  const load = useCallback(
    async (after?: string) => {
      active.current?.abort();
      const controller = new AbortController();
      active.current = controller;
      setBusy(true);
      setError("");
      try {
        const page = await api.feedbackList(
          { ...(status ? { status } : {}), ...(kind ? { kind } : {}) },
          after,
          controller.signal,
        );
        if (controller.signal.aborted) return;
        setItems((previous) =>
          after
            ? [
                ...previous,
                ...page.items.filter(
                  (item) =>
                    !previous.some((old) => old.report_id === item.report_id),
                ),
              ]
            : page.items,
        );
        setCursor(page.next_cursor);
      } catch (caught) {
        if (!controller.signal.aborted) {
          failedAfter.current = after;
          setError(errorMessage(caught));
        }
      } finally {
        if (!controller.signal.aborted) setBusy(false);
      }
    },
    [api, status, kind],
  );
  const previousLoad = useRef(load);
  useEffect(() => {
    if (previousLoad.current !== load) {
      setItems([]);
      setCursor(null);
    }
    previousLoad.current = load;
    void load();
    return () => active.current?.abort();
  }, [load, attempt]);
  useEffect(() => {
    if (reportId) setSelected(reportId);
  }, [reportId]);
  function select(id?: string) {
    setSelected(id);
    const url = new URL(window.location.href);
    url.searchParams.delete("feedback");
    url.hash = id ? `feedback/${id}` : "feedback";
    window.history.replaceState(null, "", url);
  }
  function saved(review: FeedbackReview) {
    active.current?.abort();
    setBusy(false);
    setError("");
    setItems((previous) => {
      const filters = currentFilters.current;
      const others = previous.filter(
        (item) => item.report_id !== review.report_id,
      );
      if (
        (filters.status && review.status !== filters.status) ||
        (filters.kind && review.kind !== filters.kind)
      )
        return others;
      return [...others, review].sort(
        (a, b) =>
          Date.parse(b.submitted_at) - Date.parse(a.submitted_at) ||
          a.report_id.localeCompare(b.report_id),
      );
    });
  }
  return (
    <ToolFrame
      title="Отзывы"
      subtitle="Что чинить, что придумать и за что нам уже спасибо."
    >
      <div className="feedback-privacy">
        <Icon name="shield" size={17} /> Только для владельца бота. Заметки
        видны здесь, автору ничего не отправляется.
      </div>
      <div className={`feedback-layout ${selected ? "has-selection" : ""}`}>
        <section
          className="tool-card feedback-inbox"
          aria-label="Список отзывов"
        >
          <div className="tool-card-heading">
            <h2>Входящие</h2>
            <button
              className="text-button"
              disabled={busy}
              onClick={() => setAttempt((value) => value + 1)}
              aria-label="Обновить отзывы"
            >
              <Icon name="refresh" size={16} /> Обновить
            </button>
          </div>
          <div className="feedback-filters">
            <label>
              Статус
              <select
                value={status}
                onChange={(event) =>
                  setStatus(event.target.value as ReviewStatus | "")
                }
              >
                <option value="">Все статусы</option>
                {Object.entries(reviewLabels).map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Категория
              <select
                value={kind}
                onChange={(event) =>
                  setKind(event.target.value as FeedbackKind | "")
                }
              >
                <option value="">Все категории</option>
                {Object.entries(kindLabels).map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <p className="field-hint">Сначала новые по времени отправки.</p>
          {error && (
            <div className="notice error" role="alert">
              {error}
              <button
                className="text-button"
                onClick={() => void load(failedAfter.current)}
              >
                Повторить загрузку
              </button>
            </div>
          )}
          {!busy && !error && !items.length && (
            <div className="feedback-empty">
              <Icon name="chat" size={30} />
              <h3>Пока тихо</h3>
              <p>
                Под эти фильтры отзывов нет. Новые идеи и замечания появятся
                здесь после /feedback.
              </p>
            </div>
          )}
          <ul className="feedback-list">
            {items.map((item) => (
              <li key={item.report_id}>
                <button
                  className={`feedback-item ${selected === item.report_id ? "selected" : ""}`}
                  aria-pressed={selected === item.report_id}
                  onClick={() => select(item.report_id)}
                >
                  <span className="feedback-item-meta">
                    <span className={`feedback-status ${item.status}`}>
                      {reviewLabels[item.status]}
                    </span>
                    <span>{kindLabels[item.kind]}</span>
                  </span>
                  <strong>{item.summary}</strong>
                  <span className="feedback-item-meta">
                    <span>{item.author_name}</span>
                    <time dateTime={item.submitted_at}>
                      {feedbackTime(item.submitted_at)}
                    </time>
                  </span>
                  {drafts[item.report_id] &&
                    (drafts[item.report_id]!.note !==
                      drafts[item.report_id]!.base.note ||
                      drafts[item.report_id]!.status !==
                        drafts[item.report_id]!.base.status) && (
                      <span className="feedback-unsaved">
                        Есть несохранённые изменения
                      </span>
                    )}
                </button>
              </li>
            ))}
          </ul>
          {busy && (
            <p className="tool-loading" role="status">
              Загружаем отзывы…
            </p>
          )}
          {cursor && (
            <button
              className="button secondary feedback-more"
              disabled={busy}
              onClick={() => void load(cursor)}
            >
              Загрузить ещё
            </button>
          )}
        </section>
        <section className="feedback-detail" aria-label="Карточка отзыва">
          {selected ? (
            <>
              <button
                className="text-button feedback-back"
                onClick={() => select()}
              >
                ← К списку отзывов
              </button>
              <FeedbackDetail
                key={selected}
                api={api}
                id={selected}
                draft={drafts[selected]}
                pending={pending[selected] === true}
                onPending={(value) =>
                  setPending((previous) => ({ ...previous, [selected]: value }))
                }
                onDraft={(draft) =>
                  setDrafts((previous) => ({ ...previous, [selected]: draft }))
                }
                onSaved={saved}
              />
            </>
          ) : (
            <div className="tool-card feedback-placeholder">
              <Icon name="chat" size={34} />
              <h2>Каждый отзыв — зацепка</h2>
              <p>
                Откройте карточку: внутри точный снимок, выбранный автором, и
                место для следующего шага.
              </p>
            </div>
          )}
        </section>
      </div>
    </ToolFrame>
  );
}
