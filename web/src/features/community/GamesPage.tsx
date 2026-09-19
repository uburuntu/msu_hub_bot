import { useCallback, useState } from "react";
import type { CommunityApi } from "../../platform/community";
import type { Community, GameKind } from "../../platform/communityTypes";
import { Rankings } from "./Rankings";
import { LoadState, ToolFrame } from "./ToolFrame";
import { useResource } from "./useResource";

const kinds = {
  chess: "Шахматные задачи",
  geoguess: "Геоугадайка",
  chess_play: "Шахматные партии",
} satisfies Record<GameKind, string>;
const statuses: Record<string, string> = {
  open: "Идёт игра",
  active: "Идёт игра",
  playing: "Идёт игра",
  finished: "Завершено",
  completed: "Завершено",
  expired: "Время вышло",
  cancelled: "Отменено",
  invitation: "Ждёт соперника",
  closed: "Завершено",
};
export function GamesPage({
  api,
  community,
  userId,
}: {
  api: CommunityApi;
  community: Community;
  userId: number;
}) {
  const [kind, setKind] = useState<GameKind>("chess");
  const load = useCallback(
    (signal: AbortSignal) => api.games(community.context.chat_id, kind, signal),
    [api, community.context.chat_id, kind],
  );
  const state = useResource(load);
  return (
    <ToolFrame
      title="Игровая"
      subtitle="Немного азарта. Много поводов для реванша."
      community={community}
    >
      <div className="filter-tabs tool-tabs" role="group" aria-label="Игра">
        {(Object.entries(kinds) as [GameKind, string][]).map(
          ([value, label]) => (
            <button
              key={value}
              aria-pressed={kind === value}
              className={kind === value ? "selected" : ""}
              onClick={() => setKind(value)}
            >
              {label}
            </button>
          ),
        )}
      </div>
      <LoadState busy={state.busy} error={state.error} retry={state.refresh} />
      {state.data && (
        <>
          <div className="tool-grid">
            <section className="tool-card">
              <div className="tool-card-heading">
                <h2>Таблица лидеров</h2>
                <button className="text-button" onClick={state.refresh}>
                  Обновить
                </button>
              </div>
              <Rankings
                entries={state.data.rankings}
                userId={userId}
                scoreLabel={kind === "chess_play" ? "Elo" : "очков"}
              />
            </section>
            <section className="tool-card">
              <h2>Последние игры</h2>
              {state.data.history.length ? (
                <ul className="game-history">
                  {state.data.history.map((game) => (
                    <li key={game.key}>
                      <div>
                        <strong>{game.title}</strong>
                        <span className="status-badge">
                          {statuses[game.status] ?? "Игра"}
                        </span>
                      </div>
                      <time dateTime={game.finished_at ?? game.created_at}>
                        {new Date(
                          game.finished_at ?? game.created_at,
                        ).toLocaleString("ru-RU", {
                          dateStyle: "medium",
                          timeStyle: "short",
                        })}
                      </time>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="tool-empty">
                  Свежих игр ещё нет. Сыграем в чате?
                </p>
              )}
              <p className="field-hint">
                {kind === "chess"
                  ? "/chess"
                  : kind === "geoguess"
                    ? "/geoguess"
                    : "/chess_play"}{" "}
                — начать игру в Telegram.
              </p>
            </section>
          </div>
          <p className="tool-footnote">
            Игры и рейтинг всего чата, включая темы. {state.data.retention_note}
          </p>
        </>
      )}
    </ToolFrame>
  );
}
