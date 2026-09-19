import { ApiClient, ApiError } from "./api";
import { array, boolean, integer, object, stamp, string } from "./decode";
import type {
  ChatSettings,
  ChatValues,
  Community,
  GameKind,
  GameSummary,
  Preferences,
  Ranking,
  ReactionSummary,
  Repost,
  RepostDraft,
  RepostPage,
  RepostPreview,
} from "./communityTypes";

function preferences(data: unknown): Preferences {
  const row = object(data);
  return {
    timezone: string(row.timezone),
    etag: row.etag === null ? null : string(row.etag),
  };
}
function settings(data: unknown): ChatSettings {
  const row = object(data),
    values = object(row.values);
  return {
    etag: row.etag === null ? null : string(row.etag),
    values: {
      auto_speech_recognition: boolean(values.auto_speech_recognition),
      auto_video_links: boolean(values.auto_video_links),
      with_nsfw: boolean(values.with_nsfw),
    },
  };
}
function ranking(data: unknown): Ranking {
  const row = object(data);
  return {
    user_id: integer(row.user_id),
    name: string(row.name),
    score: integer(row.score),
  };
}
function vkUrl(value: unknown): string {
  const url = string(value);
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    throw new ApiError(
      "protocol",
      "Не удалось прочитать ссылку источника.",
      503,
    );
  }
  if (
    parsed.protocol !== "https:" ||
    !["vk.com", "www.vk.com", "m.vk.com", "vk.ru"].includes(parsed.hostname) ||
    parsed.username ||
    parsed.password
  )
    throw new ApiError(
      "protocol",
      "Не удалось проверить ссылку источника.",
      503,
    );
  return url;
}
function paused(value: unknown): false {
  if (value !== false)
    throw new ApiError(
      "protocol",
      "Не удалось проверить состояние репостов.",
      503,
    );
  return false;
}
function repost(value: unknown): Repost {
  const row = object(value);
  if (row.is_suspended !== true)
    throw new ApiError("protocol", "Не удалось проверить паузу репостов.", 503);
  return {
    key: string(row.key),
    etag: string(row.etag),
    owner_id: integer(row.owner_id),
    source_url: vkUrl(row.source_url),
    chat_id: integer(row.chat_id),
    thread_id: row.thread_id === null ? null : integer(row.thread_id),
    title: string(row.title),
    with_reposts: boolean(row.with_reposts),
    with_header: boolean(row.with_header),
    include_keywords: array(row.include_keywords, string),
    exclude_keywords: array(row.exclude_keywords, string),
    last_post_id: integer(row.last_post_id),
    is_suspended: true,
    archived: boolean(row.archived),
  };
}
export class CommunityApi extends ApiClient {
  constructor(
    initData: string,
    readonly launch?: string,
    transport?: typeof fetch,
  ) {
    super(initData, transport);
  }
  private path(path: string, params: Record<string, string> = {}): string {
    const query = new URLSearchParams(params);
    if (this.launch) query.set("launch", this.launch);
    return query.size ? `${path}?${query}` : path;
  }
  community(signal?: AbortSignal): Promise<Community> {
    return this.request(
      this.path("/api/community"),
      (data) => {
        const row = object(data),
          context = object(row.context),
          access = object(row.access);
        if (row.automatic_reposts !== false)
          throw new ApiError(
            "protocol",
            "Не удалось проверить состояние репостов.",
            503,
          );
        return {
          context: {
            chat_id: integer(context.chat_id),
            thread_id:
              context.thread_id === null ? null : integer(context.thread_id),
            label: string(context.label),
          },
          access: {
            member: boolean(access.member),
            admin: boolean(access.admin),
            bot_admin: boolean(access.bot_admin),
          },
          preferences: preferences(row.preferences),
          automatic_reposts: false,
        };
      },
      undefined,
      signal,
    );
  }
  preferences(signal?: AbortSignal): Promise<Preferences> {
    return this.request("/api/preferences", preferences, undefined, signal);
  }
  savePreferences(value: Preferences): Promise<Preferences> {
    return this.request(
      "/api/preferences",
      preferences,
      value,
      undefined,
      "PATCH",
    );
  }
  chatSettings(chatId: number, signal?: AbortSignal): Promise<ChatSettings> {
    return this.request(
      this.path(`/api/chats/${chatId}/settings`),
      settings,
      undefined,
      signal,
    );
  }
  saveChatSettings(chatId: number, value: ChatSettings): Promise<ChatSettings> {
    const body: ChatValues & { etag: string | null } = {
      etag: value.etag,
      ...value.values,
    };
    return this.request(
      this.path(`/api/chats/${chatId}/settings`),
      settings,
      body,
      undefined,
      "PATCH",
    );
  }
  games(
    chatId: number,
    kind: GameKind,
    signal?: AbortSignal,
  ): Promise<GameSummary> {
    return this.request(
      this.path(`/api/chats/${chatId}/games`, { kind }),
      (data) => {
        const row = object(data);
        return {
          rankings: array(row.rankings, (value) => {
            const entry = object(value);
            return {
              ...ranking(entry),
              played: entry.played === null ? null : integer(entry.played),
              correct: entry.correct === null ? null : integer(entry.correct),
            };
          }),
          history: array(row.history, (value) => {
            const entry = object(value);
            return {
              key: string(entry.key),
              title: string(entry.title),
              status: string(entry.status),
              created_at: stamp(entry.created_at),
              finished_at:
                entry.finished_at === null ? null : stamp(entry.finished_at),
            };
          }),
          retention_note: string(row.retention_note),
        };
      },
      undefined,
      signal,
    );
  }
  reactions(
    chatId: number,
    days: 7 | 30,
    signal?: AbortSignal,
  ): Promise<ReactionSummary> {
    return this.request(
      this.path(`/api/chats/${chatId}/reactions`, { days: String(days) }),
      (data) => {
        const row = object(data);
        return {
          days: integer(row.days),
          givers: array(row.givers, ranking),
          getters: array(row.getters, ranking),
          total_reactions: integer(row.total_reactions),
          coverage_note: string(row.coverage_note),
        };
      },
      undefined,
      signal,
    );
  }
  reposts(signal?: AbortSignal): Promise<RepostPage> {
    return this.request(
      this.path("/api/reposts"),
      (data) => {
        const row = object(data);
        return {
          items: array(row.items, repost),
          automatic_posting: paused(row.automatic_posting),
        };
      },
      undefined,
      signal,
    );
  }
  createRepost(body: RepostDraft & { request_id: string }): Promise<Repost> {
    return this.request(this.path("/api/reposts"), repost, body);
  }
  saveRepost(
    item: Repost,
    draft: Omit<RepostDraft, "source"> & { archived: boolean },
  ): Promise<Repost> {
    return this.request(
      this.path(`/api/reposts/${encodeURIComponent(item.key)}`),
      repost,
      { etag: item.etag, ...draft },
      undefined,
      "PATCH",
    );
  }
  previewRepost(body: Omit<RepostDraft, "title">): Promise<RepostPreview> {
    return this.request(
      this.path("/api/reposts/preview"),
      (data) => {
        const row = object(data);
        return {
          owner_id: integer(row.owner_id),
          source_url: vkUrl(row.source_url),
          available: boolean(row.available),
          reason: string(row.reason),
          posts: array(row.posts, (data) => {
            const post = object(data);
            return {
              id: integer(post.id),
              text: string(post.text),
              url: vkUrl(post.url),
              is_repost: boolean(post.is_repost),
              selected: boolean(post.selected),
            };
          }),
          automatic_posting: paused(row.automatic_posting),
        };
      },
      body,
    );
  }
}
