import type { Session } from "./types";

export interface Preferences {
  timezone: string;
  etag: string | null;
}
export interface Community {
  context: Session["context"];
  access: { member: boolean; admin: boolean; bot_admin: boolean };
  preferences: Preferences;
  automatic_reposts: false;
}
export interface ChatValues {
  auto_speech_recognition: boolean;
  auto_video_links: boolean;
  with_nsfw: boolean;
}
export interface ChatSettings {
  etag: string | null;
  values: ChatValues;
}
export type GameKind = "chess" | "geoguess" | "chess_play";
export interface Ranking {
  user_id: number;
  name: string;
  score: number;
}
export interface GameSummary {
  rankings: (Ranking & { played: number | null; correct: number | null })[];
  history: {
    key: string;
    title: string;
    status: string;
    created_at: string;
    finished_at: string | null;
  }[];
  retention_note: string;
}
export interface ReactionSummary {
  days: number;
  givers: Ranking[];
  getters: Ranking[];
  total_reactions: number;
  coverage_note: string;
}

export interface Repost {
  key: string;
  etag: string;
  owner_id: number;
  source_url: string;
  chat_id: number;
  thread_id: number | null;
  title: string;
  with_reposts: boolean;
  with_header: boolean;
  include_keywords: string[];
  exclude_keywords: string[];
  last_post_id: number;
  is_suspended: true;
  archived: boolean;
}
export interface RepostOptions {
  with_reposts: boolean;
  with_header: boolean;
  include_keywords: string[];
  exclude_keywords: string[];
}
export interface RepostDraft extends RepostOptions {
  source: string;
  title: string;
}
export interface RepostPage {
  items: Repost[];
  automatic_posting: false;
}
export interface RepostPreview {
  owner_id: number;
  source_url: string;
  available: boolean;
  reason: string;
  posts: {
    id: number;
    text: string;
    url: string;
    is_repost: boolean;
    selected: boolean;
  }[];
  automatic_posting: false;
}
