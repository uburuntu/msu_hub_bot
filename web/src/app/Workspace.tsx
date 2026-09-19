import { useCallback, useState } from "react";
import type { ApiClient } from "../platform/api";
import { CommunityApi } from "../platform/community";
import type { Session } from "../platform/types";
import { GamesPage } from "../features/community/GamesPage";
import { ReactionsPage } from "../features/community/ReactionsPage";
import { RepostsPage } from "../features/community/RepostsPage";
import { SettingsPage } from "../features/community/SettingsPage";
import { LoadState } from "../features/community/ToolFrame";
import { useResource } from "../features/community/useResource";
import { RemindersPage } from "../features/reminders/RemindersPage";
import type { Section } from "./navigation";
import { Shell } from "./Shell";

export function Workspace({
  api,
  credentials,
  session: initial,
  launch,
}: {
  api: ApiClient;
  credentials: string;
  session: Session;
  launch?: string;
}) {
  const [session, setSession] = useState(initial);
  const [communityApi] = useState(() => new CommunityApi(credentials, launch));
  const load = useCallback(
    (signal: AbortSignal) => communityApi.community(signal),
    [communityApi],
  );
  const community = useResource(load);
  const [section, setSection] = useState<Section>("reminders");
  const [visited, setVisited] = useState<Set<Section>>(
    () => new Set(["reminders"]),
  );
  function navigate(next: Section) {
    setSection(next);
    setVisited((previous) => new Set([...previous, next]));
  }
  return (
    <Shell session={session} section={section} onNavigate={navigate}>
      <div hidden={section !== "reminders"}>
        <RemindersPage api={api} session={session} launch={launch} />
      </div>
      {section !== "reminders" && (
        <LoadState
          busy={community.busy}
          error={community.error}
          retry={community.refresh}
        />
      )}
      {community.data && (
        <>
          {visited.has("reposts") && (
            <div hidden={section !== "reposts"}>
              <RepostsPage api={communityApi} community={community.data} />
            </div>
          )}
          {visited.has("games") && (
            <div hidden={section !== "games"}>
              <GamesPage
                api={communityApi}
                community={community.data}
                userId={session.user.id}
              />
            </div>
          )}
          {visited.has("reactions") && (
            <div hidden={section !== "reactions"}>
              <ReactionsPage
                api={communityApi}
                community={community.data}
                userId={session.user.id}
              />
            </div>
          )}
          {visited.has("settings") && (
            <div hidden={section !== "settings"}>
              <SettingsPage
                api={communityApi}
                community={community.data}
                onTimezone={(timezone) =>
                  setSession((previous) => ({
                    ...previous,
                    default_timezone: timezone,
                  }))
                }
              />
            </div>
          )}
        </>
      )}
    </Shell>
  );
}
