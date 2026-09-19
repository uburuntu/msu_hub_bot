import { useCallback, useState } from "react";
import type { FormEvent } from "react";
import { ApiError, errorMessage } from "../../platform/api";
import type { CommunityApi } from "../../platform/community";
import type {
  ChatSettings,
  ChatValues,
  Community,
  Preferences,
} from "../../platform/communityTypes";
import { LoadState, ToolFrame } from "./ToolFrame";
import { useResource } from "./useResource";

const options: { key: keyof ChatValues; title: string; hint: string }[] = [
  {
    key: "auto_speech_recognition",
    title: "Расшифровывать голосовые",
    hint: "Бот добавит текст к голосовым сообщениям.",
  },
  {
    key: "auto_video_links",
    title: "Подхватывать ссылки на видео",
    hint: "Загружать поддерживаемые видео прямо в чат.",
  },
  {
    key: "with_nsfw",
    title: "Контент для взрослых",
    hint: "Разрешить команды с контентом 18+ в этом чате.",
  },
];
export function SettingsPage({
  api,
  community,
  onTimezone,
}: {
  api: CommunityApi;
  community: Community;
  onTimezone: (timezone: string) => void;
}) {
  return (
    <ToolFrame
      title="Настройки"
      subtitle="Чтобы бот был своим — и в мелочах тоже."
      community={community}
    >
      <div className="tool-grid">
        <section className="tool-card">
          <h2>Ваше время</h2>
          <TimezoneForm
            api={api}
            initial={community.preferences}
            onTimezone={onTimezone}
          />
        </section>
        {community.access.admin ? (
          <ChatSettingsPanel api={api} community={community} />
        ) : (
          <section className="tool-card">
            <h2>Настройки чата</h2>
            <p className="tool-empty">
              Менять общие настройки могут администраторы чата. Ваш часовой пояс
              доступен только вам.
            </p>
          </section>
        )}
      </div>
    </ToolFrame>
  );
}
function TimezoneForm({
  api,
  initial,
  onTimezone,
}: {
  api: CommunityApi;
  initial: Preferences;
  onTimezone: (timezone: string) => void;
}) {
  const [base, setBase] = useState(initial);
  const [timezone, setTimezone] = useState(initial.timezone);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);
  const [fresh, setFresh] = useState<Preferences>();
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy || fresh) return;
    setBusy(true);
    setError("");
    setSaved(false);
    try {
      const result = await api.savePreferences({
        etag: base.etag,
        timezone: timezone.trim(),
      });
      setBase(result);
      setTimezone(result.timezone);
      onTimezone(result.timezone);
      setSaved(true);
    } catch (caught) {
      setError(errorMessage(caught));
      if (caught instanceof ApiError && caught.status === 409) {
        try {
          setFresh(await api.preferences());
        } catch {
          setError("Не удалось получить свежие настройки. Черновик сохранён.");
        }
      }
    } finally {
      setBusy(false);
    }
  }
  return (
    <form
      className="tool-form"
      onSubmit={(event) => void submit(event)}
      aria-busy={busy}
    >
      <label>
        Часовой пояс по умолчанию
        <input
          value={timezone}
          onChange={(event) => {
            setTimezone(event.target.value);
            setSaved(false);
          }}
          list="preferred-timezones"
          autoComplete="off"
          spellCheck={false}
          required
          disabled={busy}
        />
      </label>
      <datalist id="preferred-timezones">
        {[
          "Europe/Moscow",
          "Europe/London",
          "Europe/Berlin",
          "Asia/Yekaterinburg",
          "Asia/Almaty",
          "Asia/Tbilisi",
          "UTC",
        ].map((zone) => (
          <option key={zone} value={zone} />
        ))}
      </datalist>
      <p className="field-hint">
        Для новых напоминаний. Уже созданные сохранят своё время.
      </p>
      {error && (
        <p className="notice error" role="alert">
          {error}
        </p>
      )}
      {fresh && (
        <div className="stale-review">
          <p>
            Сейчас сохранено: {fresh.timezone}. Ваш выбор: {timezone}.
          </p>
          <button
            type="button"
            className="text-button"
            onClick={() => {
              setBase(fresh);
              setFresh(undefined);
              setError("");
            }}
          >
            Сравнил, оставить мой выбор
          </button>
        </div>
      )}
      {saved && (
        <p className="success-notice" role="status">
          Часовой пояс сохранён.
        </p>
      )}
      <button className="button primary" disabled={busy || !!fresh}>
        {busy ? "Сохраняем…" : "Сохранить часовой пояс"}
      </button>
    </form>
  );
}
function ChatSettingsPanel({
  api,
  community,
}: {
  api: CommunityApi;
  community: Community;
}) {
  const load = useCallback(
    (signal: AbortSignal) =>
      api.chatSettings(community.context.chat_id, signal),
    [api, community.context.chat_id],
  );
  const state = useResource(load);
  return (
    <section className="tool-card">
      <h2>Поведение в чате</h2>
      <LoadState busy={state.busy} error={state.error} retry={state.refresh} />
      {state.data && (
        <ChatSettingsForm
          api={api}
          chatId={community.context.chat_id}
          initial={state.data}
        />
      )}
    </section>
  );
}
function ChatSettingsForm({
  api,
  chatId,
  initial,
}: {
  api: CommunityApi;
  chatId: number;
  initial: ChatSettings;
}) {
  const [base, setBase] = useState(initial);
  const [values, setValues] = useState(initial.values);
  const [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [saved, setSaved] = useState(false);
  const [fresh, setFresh] = useState<ChatSettings>();
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy || fresh) return;
    setBusy(true);
    setError("");
    setSaved(false);
    try {
      const result = await api.saveChatSettings(chatId, {
        etag: base.etag,
        values,
      });
      setBase(result);
      setValues(result.values);
      setSaved(true);
    } catch (caught) {
      setError(errorMessage(caught));
      if (caught instanceof ApiError && caught.status === 409) {
        try {
          setFresh(await api.chatSettings(chatId));
        } catch {
          setError("Не удалось получить свежие настройки. Черновик сохранён.");
        }
      }
    } finally {
      setBusy(false);
    }
  }
  return (
    <form
      className="tool-form"
      onSubmit={(event) => void submit(event)}
      aria-busy={busy}
    >
      {options.map(({ key, title, hint }) => (
        <label className="check-field" key={key}>
          <input
            type="checkbox"
            checked={values[key]}
            disabled={busy}
            onChange={(event) => {
              setValues({ ...values, [key]: event.target.checked });
              setSaved(false);
            }}
          />
          <span>
            <strong>{title}</strong>
            <small>{hint}</small>
          </span>
        </label>
      ))}
      <p className="field-hint">Действует на весь чат, включая его темы.</p>
      {error && (
        <p className="notice error" role="alert">
          {error}
        </p>
      )}
      {fresh && (
        <div className="stale-review">
          <p>Другой администратор изменил настройки. Сейчас:</p>
          <ul>
            {options.map(({ key, title }) => (
              <li key={key}>
                {title}: {fresh.values[key] ? "вкл." : "выкл."}
              </li>
            ))}
          </ul>
          <button
            type="button"
            className="text-button"
            onClick={() => {
              setBase(fresh);
              setFresh(undefined);
              setError("");
            }}
          >
            Сравнил, сохранить мой выбор
          </button>
        </div>
      )}
      {saved && (
        <p className="success-notice" role="status">
          Настройки чата сохранены.
        </p>
      )}
      <button className="button primary" disabled={busy || !!fresh}>
        {busy ? "Сохраняем…" : "Сохранить настройки чата"}
      </button>
    </form>
  );
}
