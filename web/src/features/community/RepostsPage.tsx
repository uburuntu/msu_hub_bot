import { useCallback, useState } from "react";
import type { CommunityApi } from "../../platform/community";
import type { Community, Repost } from "../../platform/communityTypes";
import { Dialog } from "../../ui/Dialog";
import { Icon } from "../../ui/Icon";
import { RepostForm } from "./RepostForm";
import { LoadState, ToolFrame } from "./ToolFrame";
import { useResource } from "./useResource";

export function RepostsPage({
  api,
  community,
}: {
  api: CommunityApi;
  community: Community;
}) {
  if (!community.access.admin)
    return (
      <ToolFrame
        title="Репосты"
        subtitle="Интересное из VK — в свой чат."
        community={community}
      >
        <div className="tool-card">
          <h2>Для администраторов чата</h2>
          <p className="tool-empty">
            Откройте /app в чате, которым вы управляете. Здесь можно подготовить
            источники и фильтры; автоматическая отправка выключена.
          </p>
        </div>
      </ToolFrame>
    );
  return <ManagedReposts api={api} community={community} />;
}
function ManagedReposts({
  api,
  community,
}: {
  api: CommunityApi;
  community: Community;
}) {
  const load = useCallback((signal: AbortSignal) => api.reposts(signal), [api]);
  const state = useResource(load);
  const [editing, setEditing] = useState<Repost>();
  const [busy, setBusy] = useState(false);
  const [archive, setArchive] = useState(false);
  const [notice, setNotice] = useState("");
  function saved(item: Repost) {
    state.setData((previous) =>
      previous
        ? {
            ...previous,
            items: [
              ...previous.items.filter((entry) => entry.key !== item.key),
              item,
            ],
          }
        : { items: [item], automatic_posting: false },
    );
    setNotice("Источник сохранён. Отправка остаётся на паузе.");
  }
  const items =
    state.data?.items.filter((entry) => entry.archived === archive) ?? [];
  return (
    <ToolFrame
      title="Репосты"
      subtitle="Соберите свои источники. Лишнее отсеем фильтрами."
      community={community}
    >
      <div className="intro-strip">
        <Icon name="shield" size={22} />
        <p>
          Все публикации на паузе.
          <strong>
            {" "}
            Настройки сохранены. Добавление и предпросмотр ничего не отправляют
            в чат.
          </strong>
        </p>
      </div>
      {!community.access.bot_admin && (
        <p className="notice">
          Боту могут понадобиться права на отправку сообщений, когда публикации
          будут включены.
        </p>
      )}
      {notice && (
        <p className="success-notice" role="status">
          {notice}
        </p>
      )}
      <div className="tool-grid repost-layout">
        <section className="tool-card">
          <h2>Добавить источник</h2>
          <RepostForm
            api={api}
            destination={community.context.label}
            onSaved={saved}
          />
        </section>
        <section className="tool-card">
          <div className="tool-card-heading">
            <h2>Источники чата</h2>
            <button
              className="icon-button"
              aria-label="Обновить источники"
              disabled={state.busy}
              onClick={state.refresh}
            >
              <Icon name="refresh" size={18} />
            </button>
          </div>
          <div
            className="filter-tabs tool-tabs"
            role="group"
            aria-label="Статус источников"
          >
            <button
              className={!archive ? "selected" : ""}
              aria-pressed={!archive}
              onClick={() => setArchive(false)}
            >
              На паузе
            </button>
            <button
              className={archive ? "selected" : ""}
              aria-pressed={archive}
              onClick={() => setArchive(true)}
            >
              Архив
            </button>
          </div>
          <LoadState
            busy={state.busy}
            error={state.error}
            retry={state.refresh}
          />
          {state.data &&
            (items.length ? (
              <ul className="repost-list">
                {items.map((item) => (
                  <li key={item.key}>
                    <div className="tool-card-heading">
                      <strong>{item.title || `VK ${item.owner_id}`}</strong>
                      <span className="status-badge">
                        {item.archived ? "В архиве" : "На паузе"}
                      </span>
                    </div>
                    <a
                      href={item.source_url}
                      target="_blank"
                      rel="noopener noreferrer"
                    >
                      {item.source_url}
                    </a>
                    <p>
                      {item.with_reposts ? "С репостами" : "Только свои записи"}{" "}
                      · {item.with_header ? "Со ссылкой" : "Без заголовка"}
                    </p>
                    {item.include_keywords.length > 0 && (
                      <p>Ищем: {item.include_keywords.join(", ")}</p>
                    )}
                    {item.exclude_keywords.length > 0 && (
                      <p>Исключаем: {item.exclude_keywords.join(", ")}</p>
                    )}
                    <button
                      className="button secondary"
                      onClick={() => setEditing(item)}
                    >
                      Настроить {item.title || "источник"}
                    </button>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="tool-empty">
                {archive
                  ? "В архиве пока пусто."
                  : "Добавьте первый источник — любимую группу, новости факультета или события рядом."}
              </p>
            ))}
        </section>
      </div>
      {editing && (
        <Dialog
          title="Настроить источник"
          busy={busy}
          onClose={() => setEditing(undefined)}
        >
          <div className="tool-dialog-content">
            <RepostForm
              api={api}
              item={editing}
              destination={community.context.label}
              onBusy={setBusy}
              onSaved={(item) => {
                saved(item);
                setEditing(undefined);
              }}
            />
          </div>
        </Dialog>
      )}
    </ToolFrame>
  );
}
