export const sections = {
  reminders: { label: "Напоминания", icon: "bell" },
  reposts: { label: "Репосты", icon: "send" },
  games: { label: "Игровая", icon: "trophy" },
  reactions: { label: "Реакции", icon: "heart" },
  settings: { label: "Настройки", icon: "settings" },
} as const;
export type Section = keyof typeof sections;
