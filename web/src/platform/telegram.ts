interface TelegramApp {
  initData: string;
  colorScheme: "light" | "dark";
  ready(): void;
  expand(): void;
  onEvent(event: string, callback: () => void): void;
  offEvent(event: string, callback: () => void): void;
}

declare global {
  interface Window {
    Telegram?: { WebApp?: TelegramApp };
  }
}

export function telegramCredentials(): string {
  return window.Telegram?.WebApp?.initData ?? "";
}

export function launchToken(): string | undefined {
  return new URLSearchParams(window.location.search).get("launch") || undefined;
}

export function connectTelegram(): () => void {
  const app = window.Telegram?.WebApp;
  if (!app?.initData) return () => {};
  const theme = () => {
    document.documentElement.dataset.theme = app.colorScheme;
  };
  theme();
  app.ready();
  app.expand();
  app.onEvent("themeChanged", theme);
  return () => app.offEvent("themeChanged", theme);
}
