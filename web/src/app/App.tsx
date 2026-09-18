import { useEffect, useState } from "react";
import { ApiClient, ApiError, errorMessage } from "../platform/api";
import type { Session } from "../platform/types";
import {
  connectTelegram,
  launchToken,
  telegramCredentials,
} from "../platform/telegram";
import { Icon } from "../ui/Icon";
import { RemindersPage } from "../features/reminders/RemindersPage";
import { Shell } from "./Shell";

export function App() {
  const [credentials] = useState(telegramCredentials);
  const [launch] = useState(launchToken);
  const [api] = useState(() => new ApiClient(credentials));
  const [session, setSession] = useState<Session>();
  const [error, setError] = useState<unknown>(null);
  const [attempt, setAttempt] = useState(0);
  useEffect(connectTelegram, []);
  useEffect(() => {
    if (!credentials) return;
    const controller = new AbortController();
    setError(null);
    void api
      .session(launch, controller.signal)
      .then((value) => {
        if (!controller.signal.aborted) setSession(value);
      })
      .catch((caught: unknown) => {
        if (!controller.signal.aborted) setError(caught);
      });
    return () => controller.abort();
  }, [api, credentials, launch, attempt]);

  if (!credentials || (error instanceof ApiError && error.status === 401))
    return (
      <Shell>
        <div className="welcome">
          <span className="welcome-icon">
            <Icon name="bell" size={40} />
          </span>
          <div className="eyebrow">MSU HUB В TELEGRAM</div>
          <h1>Важное не потеряется.</h1>
          <p>
            Дела, планы и маленькие «не забыть».
            <br />
            Откройте приложение из бота, чтобы управлять своими напоминаниями.
          </p>
          <a className="button primary" href="https://t.me/msu_hub_bot">
            <Icon name="send" />
            Открыть бота
          </a>
          <span className="welcome-hint">
            Уже в Telegram? Закройте это окно и откройте приложение снова.
          </span>
        </div>
      </Shell>
    );
  return (
    <Shell session={session}>
      {session ? (
        <RemindersPage api={api} session={session} launch={launch} />
      ) : (
        <div className="session-loading">
          {error ? (
            <>
              <span className="welcome-icon">
                <Icon name="refresh" size={28} />
              </span>
              <h1>Давайте ещё раз</h1>
              <p role="alert">{errorMessage(error)}</p>
              <button
                className="button primary"
                onClick={() => setAttempt((value) => value + 1)}
              >
                Повторить
              </button>
            </>
          ) : (
            <>
              <span className="welcome-icon loading-pulse">
                <Icon name="bell" size={32} />
              </span>
              <p role="status">Открываем ваши напоминания…</p>
            </>
          )}
        </div>
      )}
    </Shell>
  );
}
