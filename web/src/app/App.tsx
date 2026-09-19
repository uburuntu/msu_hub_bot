import { useEffect, useState } from "react";
import { ApiClient, ApiError, errorMessage } from "../platform/api";
import type { Session } from "../platform/types";
import {
  connectTelegram,
  launchToken,
  telegramCredentials,
} from "../platform/telegram";
import { Icon } from "../ui/Icon";
import { Workspace } from "./Workspace";
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
          <h1>Свой бот для всего.</h1>
          <p>
            Напоминания, игры и маленькие заботы чата.
            <br />
            Откройте приложение из бота, чтобы всё было под рукой.
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
  if (session)
    return (
      <Workspace
        api={api}
        credentials={credentials}
        session={session}
        launch={launch}
      />
    );
  return (
    <Shell session={session}>
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
            <p role="status">Открываем ваши инструменты…</p>
          </>
        )}
      </div>
    </Shell>
  );
}
