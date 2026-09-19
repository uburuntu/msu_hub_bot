import { useId, useRef, useState } from "react";
import type { FormEvent } from "react";
import { ApiError, errorMessage } from "../../platform/api";
import type { CommunityApi } from "../../platform/community";
import type {
  Repost,
  RepostDraft,
  RepostPreview,
} from "../../platform/communityTypes";

function words(value: string): string[] {
  return [
    ...new Set(
      value
        .split(",")
        .map((entry) => entry.trim())
        .filter(Boolean),
    ),
  ];
}
export function RepostForm({
  api,
  item,
  destination,
  onSaved,
  onBusy,
}: {
  api: CommunityApi;
  item?: Repost;
  destination: string;
  onSaved: (item: Repost) => void;
  onBusy?: (busy: boolean) => void;
}) {
  const id = useId();
  const [base, setBase] = useState(item);
  const [source, setSource] = useState(item?.source_url ?? "");
  const [title, setTitle] = useState(item?.title ?? "");
  const [withReposts, setWithReposts] = useState(item?.with_reposts ?? false);
  const [withHeader, setWithHeader] = useState(item?.with_header ?? true);
  const [include, setInclude] = useState(
    item?.include_keywords.join(", ") ?? "",
  );
  const [exclude, setExclude] = useState(
    item?.exclude_keywords.join(", ") ?? "",
  );
  const [archived, setArchived] = useState(item?.archived ?? false);
  const [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const [uncertain, setUncertain] = useState(false);
  const [fresh, setFresh] = useState<Repost>();
  const [preview, setPreview] = useState<{
    fingerprint: string;
    result: RepostPreview;
  }>();
  const pending = useRef<(RepostDraft & { request_id: string }) | null>(null);
  const draft: RepostDraft = {
    source: source.trim(),
    title: title.trim(),
    with_reposts: withReposts,
    with_header: withHeader,
    include_keywords: words(include),
    exclude_keywords: words(exclude),
  };
  const fingerprint = JSON.stringify(draft);
  const shownPreview =
    preview?.fingerprint === fingerprint ? preview.result : undefined;
  const locked = busy || uncertain;
  function working(value: boolean) {
    setBusy(value);
    onBusy?.(value);
  }
  async function showPreview() {
    if (busy || !draft.source) return;
    working(true);
    setError("");
    try {
      const { title: _title, ...body } = draft;
      setPreview({ fingerprint, result: await api.previewRepost(body) });
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      working(false);
    }
  }
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy || fresh) return;
    setError("");
    if (
      [draft.include_keywords, draft.exclude_keywords].some(
        (entries) =>
          entries.length > 20 || entries.some((word) => word.length > 80),
      )
    ) {
      setError(
        "В каждом фильтре — до 20 слов или фраз, до 80 знаков в каждой.",
      );
      return;
    }
    working(true);
    try {
      let result: Repost;
      if (base) {
        const { source: _source, ...editable } = draft;
        result = await api.saveRepost(base, { ...editable, archived });
      } else {
        pending.current ??= { ...draft, request_id: crypto.randomUUID() };
        result = await api.createRepost(pending.current);
      }
      pending.current = null;
      setUncertain(false);
      if (!base) {
        setSource("");
        setTitle("");
        setInclude("");
        setExclude("");
        setPreview(undefined);
      }
      onSaved(result);
    } catch (caught) {
      setError(errorMessage(caught));
      if (caught instanceof ApiError && caught.status === 409 && base) {
        try {
          const latest = (await api.reposts()).items.find(
            (entry) => entry.key === base.key,
          );
          if (latest) setFresh(latest);
          else setError("Этот источник больше недоступен. Черновик сохранён.");
        } catch {
          setError("Не удалось проверить свежую версию. Черновик сохранён.");
        }
      } else if (!base && caught instanceof ApiError && caught.uncertain)
        setUncertain(true);
      else if (!base) pending.current = null;
    } finally {
      working(false);
    }
  }
  return (
    <form
      className="tool-form"
      onSubmit={(event) => void submit(event)}
      aria-busy={busy}
    >
      <fieldset disabled={locked}>
        <label htmlFor={`${id}-source`}>
          Источник VK
          <input
            id={`${id}-source`}
            value={source}
            onChange={(event) => setSource(event.target.value)}
            placeholder="https://vk.com/club123 или -123"
            required
            maxLength={256}
            readOnly={!!base}
            autoComplete="off"
            spellCheck={false}
          />
        </label>
        <label htmlFor={`${id}-title`}>
          Название для себя
          <input
            id={`${id}-title`}
            value={title}
            onChange={(event) => setTitle(event.target.value)}
            placeholder="Новости факультета"
            maxLength={160}
          />
        </label>
        <label className="check-field">
          <input
            type="checkbox"
            checked={withReposts}
            onChange={(event) => setWithReposts(event.target.checked)}
          />
          <span>
            <strong>Включать репосты источника</strong>
            <small>Иначе — только его собственные публикации.</small>
          </span>
        </label>
        <label className="check-field">
          <input
            type="checkbox"
            checked={withHeader}
            onChange={(event) => setWithHeader(event.target.checked)}
          />
          <span>
            <strong>Добавлять ссылку на источник</strong>
            <small>Заголовок помогает понять, откуда новость.</small>
          </span>
        </label>
        <details className="repost-filters">
          <summary>Фильтры по словам</summary>
          <label>
            Есть хотя бы одно слово
            <input
              value={include}
              onChange={(event) => setInclude(event.target.value)}
              placeholder="лекция, встреча, регистрация"
              maxLength={1800}
            />
          </label>
          <label>
            Нет ни одного слова
            <input
              value={exclude}
              onChange={(event) => setExclude(event.target.value)}
              placeholder="реклама, конкурс"
              maxLength={1800}
            />
          </label>
          <p className="field-hint">
            Через запятую, без учёта регистра. Пустые поля пропускают все
            публикации.
          </p>
        </details>
        {base && (
          <label className="check-field">
            <input
              type="checkbox"
              checked={archived}
              onChange={(event) => setArchived(event.target.checked)}
            />
            <span>
              <strong>Убрать в архив</strong>
              <small>Сохранить настройки и скрыть из основного списка.</small>
            </span>
          </label>
        )}
      </fieldset>
      <div className="destination">
        <div>
          <span>Куда будут приходить публикации</span>
          <strong>{destination}</strong>
        </div>
      </div>
      <p className="notice">
        Сохраняем на паузе. Публикации в чат не отправляются.
      </p>
      {error && (
        <p className="notice error" role="alert">
          {error}
        </p>
      )}
      {uncertain && (
        <p className="notice">
          Источник мог сохраниться. Повторим тот же запрос без дубликатов;
          черновик пока зафиксирован.
        </p>
      )}
      {fresh && (
        <div className="stale-review">
          <strong>Свежая версия: {fresh.title || fresh.source_url}</strong>
          <p>
            {fresh.with_reposts ? "С репостами" : "Без репостов"} ·{" "}
            {fresh.with_header ? "Со ссылкой" : "Без ссылки"} ·{" "}
            {fresh.archived ? "В архиве" : "На паузе"}
          </p>
          <p>
            Включить: {fresh.include_keywords.join(", ") || "всё"}. Исключить:{" "}
            {fresh.exclude_keywords.join(", ") || "ничего"}.
          </p>
          <button
            className="text-button"
            type="button"
            onClick={() => {
              setBase(fresh);
              setFresh(undefined);
              setError("");
            }}
          >
            Сравнил, сохранить мой черновик
          </button>
        </div>
      )}
      <div className="form-actions">
        <button
          type="button"
          className="button secondary"
          disabled={locked || !draft.source}
          onClick={() => void showPreview()}
        >
          Посмотреть публикации
        </button>
        <button className="button primary" disabled={busy || !!fresh}>
          {busy
            ? "Проверяем…"
            : uncertain
              ? "Проверить сохранение"
              : base
                ? "Сохранить изменения"
                : "Добавить на паузе"}
        </button>
      </div>
      {shownPreview && (
        <section
          className="repost-preview"
          aria-label="Предпросмотр публикаций"
        >
          <h3>Как сработают фильтры</h3>
          <p className="field-hint">{shownPreview.reason}</p>
          {!shownPreview.available && (
            <p className="notice">
              Предпросмотр недоступен. Настройки можно сохранить и проверить
              позже.
            </p>
          )}
          {shownPreview.posts.map((post) => (
            <article
              key={post.id}
              className={
                post.selected ? "preview-selected" : "preview-filtered"
              }
            >
              <span className="status-badge">
                {post.selected ? "Подходит" : "Отсеяно фильтром"}
                {post.is_repost ? " · репост" : ""}
              </span>
              <p>{post.text || "Публикация без текста"}</p>
              <a href={post.url} target="_blank" rel="noopener noreferrer">
                Открыть в VK ↗
              </a>
            </article>
          ))}
          <p className="field-hint">
            Это проверка источника. В Telegram ничего не отправлено.
          </p>
        </section>
      )}
    </form>
  );
}
