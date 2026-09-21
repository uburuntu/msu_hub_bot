import { useCallback, useEffect, useState } from "react";
import type { ApiClient } from "../platform/api";
import { CommunityApi } from "../platform/community";
import { FeedbackApi } from "../platform/feedback";
import { FeedbackPage } from "../features/feedback/FeedbackPage";
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

function feedbackLocation(): { reportId?: string } | null {
  const route = /^#feedback(?:\/([a-f0-9]{16}))?$/.exec(window.location.hash);
  if (route) return { reportId: route[1] };
  const values = new URLSearchParams(window.location.search).getAll("feedback");
  return values.length === 1 && /^[a-f0-9]{16}$/.test(values[0]!)
    ? { reportId: values[0] }
    : null;
}

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
  const [feedbackApi] = useState(() => new FeedbackApi(credentials));
  const canReview = session.capabilities?.feedback_review === true;
  const [feedbackLink, setFeedbackLink] = useState(() =>
    canReview ? feedbackLocation() : null,
  );
  const load = useCallback(
    (signal: AbortSignal) => communityApi.community(signal),
    [communityApi],
  );
  const community = useResource(load);
  const [section, setSection] = useState<Section>(
    feedbackLink ? "feedback" : "reminders",
  );
  const [visited, setVisited] = useState<Set<Section>>(
    () => new Set([feedbackLink ? "feedback" : "reminders"]),
  );
  useEffect(() => {
    const changed = () => {
      const match = canReview ? feedbackLocation() : null;
      if (!match) return;
      setFeedbackLink(match);
      setSection("feedback");
      setVisited((previous) => new Set([...previous, "feedback"]));
    };
    window.addEventListener("hashchange", changed);
    return () => window.removeEventListener("hashchange", changed);
  }, [canReview]);
  function navigate(next: Section) {
    if (next === "feedback" && !canReview) return;
    setSection(next);
    setVisited((previous) => new Set([...previous, next]));
    const url = new URL(window.location.href);
    url.searchParams.delete("feedback");
    url.hash = next === "feedback" ? "feedback" : "";
    window.history.replaceState(null, "", url);
  }
  return (
    <Shell session={session} section={section} onNavigate={navigate}>
      <div hidden={section !== "reminders"}>
        <RemindersPage api={api} session={session} launch={launch} />
      </div>
      {canReview && visited.has("feedback") && (
        <div hidden={section !== "feedback"}>
          <FeedbackPage api={feedbackApi} reportId={feedbackLink?.reportId} />
        </div>
      )}
      {section !== "reminders" && section !== "feedback" && (
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
