export const reviewLabels = {
  new: "Новый",
  in_progress: "В работе",
  done: "Готово",
  dismissed: "Не планируем",
} as const;
export const kindLabels = {
  bug: "Ошибка",
  idea: "Идея",
  other: "Другое",
} as const;
export function feedbackTime(value: string): string {
  return new Intl.DateTimeFormat("ru-RU", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}
