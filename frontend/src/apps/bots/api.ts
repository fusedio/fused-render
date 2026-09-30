// Typed bridge to the Bots router (fused_render/server/routers/bots.py) — the
// shared transport (`getJson`/`postJson`: thrown HttpError, X-Fused guard). PUT
// and DELETE have no exported helper in @platform/lib/api, so they go through
// the one local `send` below with the same guard header and error contract.
import {
  getJson,
  getTasks,
  postJson,
  type DefaultModel,
  type HttpError,
  type SessionEffort,
  type Task,
} from "@platform/lib/api";

/** bot.json plus the bot's folder (`path`), which is also every chat's cwd. */
export interface Bot {
  name: string;
  /** Minted once at creation, never renamed: it IS the folder name, and the
   *  Claude transcripts for this bot live under a munge of that folder. */
  slug: string;
  persona: string;
  emoji: string;
  color: string;
  model: DefaultModel;
  effort: SessionEffort;
  created: string;
  updated: string;
  path: string;
}

/** One app task a bot started, with the schedule entry's live fields joined
 *  in by the server (they are never stored in tasks.json). */
export interface BotTask {
  id: string;
  kind: "new" | "edit";
  app_name: string;
  app_path: string;
  entry_html: string;
  spec: string;
  run_id: string;
  claude_session_id: string;
  created: string;
  /** "" = the schedule entry is gone (status unknown). */
  state?: string;
  turn?: string;
  /** The failed run's error text, when there is one. */
  error?: string;
}

export interface BotInput {
  name: string;
  persona: string;
  emoji: string;
  color: string;
  model: DefaultModel;
  effort: SessionEffort;
}

async function send<T>(method: "PUT" | "DELETE", url: string, body?: unknown): Promise<T> {
  const res = await fetch(url, {
    method,
    headers: { "Content-Type": "application/json", "X-Fused": "1" },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  let data: { error?: string } | null = null;
  try {
    data = await res.json();
  } catch {
    data = null;
  }
  if (!res.ok) {
    const err = new Error((data && data.error) || `HTTP ${res.status}`) as HttpError;
    err.status = res.status;
    throw err;
  }
  return data as T;
}

const at = (slug: string) => `/api/bots/${encodeURIComponent(slug)}`;

export const listBots = () => getJson<{ root: string; bots: Bot[] }>("/api/bots");

export const createBot = (input: BotInput) => postJson<{ bot: Bot }>("/api/bots", input);

export const updateBot = (slug: string, patch: Partial<BotInput>) =>
  send<{ bot: Bot }>("PUT", at(slug), patch);

export const deleteBot = (slug: string) => send<{ ok: boolean }>("DELETE", at(slug));

export const getMemory = (slug: string) => getJson<{ content: string }>(`${at(slug)}/memory`);

export const putMemory = (slug: string, content: string) =>
  send<{ ok: boolean }>("PUT", `${at(slug)}/memory`, { content });

export const listBotTasks = (slug: string) =>
  getJson<{ tasks: BotTask[] }>(`${at(slug)}/tasks`);

const trimSlash = (p: string) => p.replace(/\/+$/, "");

/**
 * THE BOT'S CHATS — the Claude sessions whose cwd is the bot folder.
 *
 * Read off `GET /api/tasks`, not `GET /api/claude-sessions`: that endpoint
 * lists FOLDERS (one row per cwd, no session ids, no `?file=` filter), while
 * the tasks listing is one row per session with the title, the last message
 * and `last_active` already decided server-side — the same rows the chat's own
 * Recent list draws. Filtered to this folder exactly (a bot folder holds no
 * other projects), drafts and never-run entries dropped.
 */
export async function listBotSessions(botPath: string): Promise<Task[]> {
  const here = trimSlash(botPath);
  const { tasks } = await getTasks();
  return tasks
    .filter(
      (t) =>
        t.kind !== "draft" &&
        !!t.session_id &&
        (trimSlash(t.project || "") === here || trimSlash(t.target || "") === here),
    )
    .sort((a, b) => (b.last_active || 0) - (a.last_active || 0));
}
