import type { Ranking } from "../../platform/communityTypes";

export function Rankings({
  entries,
  scoreLabel,
  userId,
}: {
  entries: Ranking[];
  scoreLabel: string;
  userId: number;
}) {
  return entries.length ? (
    <ol className="ranking-list" aria-label={scoreLabel}>
      {entries.map((entry, index) => (
        <li
          key={entry.user_id}
          className={entry.user_id === userId ? "is-you" : ""}
        >
          <span className={`rank-place place-${index + 1}`}>{index + 1}</span>
          <span className="rank-name">
            {entry.name}
            {entry.user_id === userId && <small>это вы</small>}
          </span>
          <strong>
            {entry.score.toLocaleString("ru-RU")}
            <small>{scoreLabel}</small>
          </strong>
        </li>
      ))}
    </ol>
  ) : (
    <p className="tool-empty">Пока пусто. Самое время начать!</p>
  );
}
