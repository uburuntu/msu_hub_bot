import { useCallback, useState } from "react";
import type { CommunityApi } from "../../platform/community";
import type { Community } from "../../platform/communityTypes";
import { Rankings } from "./Rankings";
import { LoadState, ToolFrame } from "./ToolFrame";
import { useResource } from "./useResource";

export function ReactionsPage({
  api,
  community,
  userId,
}: {
  api: CommunityApi;
  community: Community;
  userId: number;
}) {
  const [days, setDays] = useState<7 | 30>(30);
  const load = useCallback(
    (signal: AbortSignal) =>
      api.reactions(community.context.chat_id, days, signal),
    [api, community.context.chat_id, days],
  );
  const state = useResource(load);
  return (
    <ToolFrame
      title="Реакции"
      subtitle="Кто поднимает настроение и кто не жалеет сердечек."
      community={community}
    >
      <div
        className="filter-tabs tool-tabs"
        role="group"
        aria-label="Период реакций"
      >
        {([7, 30] as const).map((value) => (
          <button
            key={value}
            aria-pressed={days === value}
            className={days === value ? "selected" : ""}
            onClick={() => setDays(value)}
          >
            {value} дней
          </button>
        ))}
      </div>
      <LoadState busy={state.busy} error={state.error} retry={state.refresh} />
      {state.data && (
        <>
          <div className="intro-strip">
            <p>
              <b>{state.data.total_reactions.toLocaleString("ru-RU")}</b>{" "}
              поводов улыбнуться за {days} дней.{" "}
              <strong>
                Один человек + одно сообщение = одно очко. Реакции себе не
                считаются.
              </strong>
            </p>
          </div>
          <div className="tool-grid">
            <section className="tool-card">
              <h2>Собирают реакции</h2>
              <Rankings
                entries={state.data.getters}
                scoreLabel="получено"
                userId={userId}
              />
            </section>
            <section className="tool-card">
              <h2>Дарят реакции</h2>
              <Rankings
                entries={state.data.givers}
                scoreLabel="подарено"
                userId={userId}
              />
            </section>
          </div>
          <p className="tool-footnote">{state.data.coverage_note}</p>
          <button className="button secondary" onClick={state.refresh}>
            Обновить реакции
          </button>
        </>
      )}
    </ToolFrame>
  );
}
