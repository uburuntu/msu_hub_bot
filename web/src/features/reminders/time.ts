export function dateInput(iso: string, timezone: string): string {
  const parts = new Intl.DateTimeFormat("sv-SE", {
    timeZone: timezone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).formatToParts(new Date(iso));
  const part = (type: Intl.DateTimeFormatPartTypes) =>
    parts.find((value) => value.type === type)?.value ?? "";
  return `${part("year")}-${part("month")}-${part("day")}T${part("hour")}:${part("minute")}`;
}

export function tomorrowSchedule(timezone: string, now = new Date()): string {
  const today = dateInput(now.toISOString(), timezone).slice(0, 10);
  const next = new Date(`${today}T12:00:00Z`);
  next.setUTCDate(next.getUTCDate() + 1);
  return `at ${next.toISOString().slice(0, 10)} 09:00`;
}

export function dueLabel(iso: string, timezone: string): string {
  try {
    return new Intl.DateTimeFormat("ru-RU", {
      timeZone: timezone,
      day: "numeric",
      month: "short",
      hour: "2-digit",
      minute: "2-digit",
    }).format(new Date(iso));
  } catch {
    return (
      new Intl.DateTimeFormat("ru-RU", {
        timeZone: "UTC",
        dateStyle: "medium",
        timeStyle: "short",
      }).format(new Date(iso)) + " UTC"
    );
  }
}

export function zoneLabel(timezone: string): string {
  return (
    (
      {
        "Europe/Moscow": "Москва",
        "Europe/London": "Лондон",
        "Europe/Berlin": "Берлин",
        "Asia/Yekaterinburg": "Екатеринбург",
        "Asia/Almaty": "Алматы",
        "Asia/Tbilisi": "Тбилиси",
        UTC: "UTC",
      } as Record<string, string>
    )[timezone] || timezone
  );
}
