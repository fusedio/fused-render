// Typed fetch wrappers for every route in docs/BOT-APP.md §3 (OpenBot's `fused.daemon.run({action…})` calls).
// Every call goes through request(): one place counts calls in flight, reports a call still pending after STALL_MS
// (the store shows it in the banner) and logs anything slower than SLOW_MS. The store installs those hooks
// (apiHooks) at load, so this module imports nothing from it.

// ------------------------------------------------------------------ wire types (§2) ----
/** `note`: a muted harness line in the thread ("Already ran …", "Still waiting for Approve / Deny on …"); not a bubble, not reactable. */
export type Role = "user" | "thought" | "action" | "approval" | "question" | "done" | "error" | "system" | "note" | "delivery";
export type BotStatus = "idle" | "running" | "waiting" | "paused" | "error";
export type Model = "haiku" | "sonnet" | "opus" | "fable" | "local-4b" | "local-9b";
export type Effort = "low" | "medium" | "high" | "xhigh";

export interface Offer { kind: "use" | "build"; name: string; dir: string; spec: string }
export interface AppRef { name: string; dir: string; params?: Record<string, string> | string; tools?: unknown }
export interface ReplyRef { seq: number; role: Role; text: string }

/** A channel address (docs §10): "imessage" + the sender's handle, "routine", "botsend", "handoff" + the Super Bot's id
 *  (a task Super Bot delegated, docs §11); the web is never stamped. */
export interface Via { kind: "imessage" | "routine" | "botsend" | "handoff" | "web" | (string & {}); addr: string }

export type HandoffState = "received" | "working" | "blocked" | "done" | "failed" | "cancelled";
/** Super Bot's meta["handoffs"] (docs §11): one task it handed to an ordinary bot, last 40. */
export interface Handoff {
  id: string; target: string; target_name: string; task: string; origin_via?: Via | null; created_at: number;
  state: HandoffState; started_at?: number; done_at?: number; result?: string; updated_at?: number;
  /** While `blocked`: what the bot waits on the user for at the Mac (the card's text, <= 300 chars). */
  blocked?: { kind: string; text: string };
  /** The bot's last few `note` progress lines (<= 5, <= 160 chars each). */
  notes?: string[];
}
/** The `handoff` stamp on Super Bot's hand-off lines ("Asked …", "… needs you at the laptop", the result). */
export interface HandoffRef { id: string; target: string; target_name: string; task?: string; state: HandoffState; task_dir?: string }

export interface BotEvent {
  seq: number;
  ts: number;
  role: Role;
  text: string;
  /** action: a short human one-liner (<= 160 chars). */
  result?: string;
  /** "<seq>.jpg" — load it through stepThumbUrl(botId, thumb). */
  thumb?: string;
  /** action: the full raw tool result (<= 1500 chars), shown when the chip is opened. Notes may carry one too (unused). */
  detail?: unknown;
  options?: string[];
  offer?: Offer;
  app?: AppRef;
  reply?: ReplyRef;
  /** Where the message came from / which channel the bot's reply went to (docs §10). Absent = the web page. */
  via?: Via;
  /** "build": a build watcher's notice. "handoff": a hand-off line (docs §11): on Super Bot it carries `handoff` and
   *  renders as a hand-off card; on the target bot it is the bare "Sent to Super Bot: …" line. */
  source?: "build" | "handoff" | (string & {});
  handoff?: HandoffRef;
  /** A "Sent to Super Bot" line: the card it became in Super Bot's chat ("Read more" opens that chat there). */
  link?: { bot: string; seq: number };
  /** done / question: the bot's own phone-sized version (D11); the router texts this instead of the cut message. */
  summary?: string;
  /** role "delivery" (D12): the event this row records a send of, the channel, the address, and the error when the send failed.
   *  `text` is exactly what went out. Never rendered as a bubble; the thread joins it to `ref`. */
  ref?: number;
  channel?: string;
  addr?: string;
  error?: string;
  trace?: unknown;
  artifacts?: unknown;
}

export interface Tab { i: number; id: string; title: string; url: string; active: boolean; ws: string }
export interface FileRow { name: string; path: string; size: number; kind: string; ts?: number }
export interface Artifact { ts: number; kind: "save" | "download" | "build" | string; name: string; path: string; size?: number; task?: string; link?: string; title?: string }

export interface BrowserState {
  running: boolean;
  url?: string;
  title?: string;
  visible?: boolean;
  sealed?: boolean;
  encrypt?: boolean;
  tabs?: Tab[];
  files?: FileRow[];
  artifacts?: Artifact[];
  artifacts_dir?: string;
}

/** meta["routines"] in agents.py. Times are epoch seconds; weekdays Mon=0. */
export interface Routine {
  id: string;
  task: string;
  kind: "interval" | "daily" | "once";
  minutes?: number;
  time?: string;
  weekdays?: number[];
  at?: number;
  enabled: boolean;
  next?: number;
  last?: number;
  last_result?: string;
  last_message?: string;
  fails?: number;
}

/** Bot.skills(): one playbook file. `name` is the file stem (the rid in /skills calls). */
export interface Skill { name: string; title: string; trigger: string; body: string }

/** `icon`: a brand key (lib/face.ts BRANDS) for a preset bot's disc avatar; "" or absent for a blob face. */
export interface Face { shape?: string; color?: string; icon?: string }

export interface Bot {
  id: string;
  name: string;
  model: Model | string;
  effort: Effort | string;
  status: BotStatus;
  instructions?: string;
  created?: number;
  task?: string;
  step?: number;
  /** The step budget of the current task; the status line shows "step 12/60" when present. */
  step_cap?: number;
  /** While `status === "waiting"`: the seq of the approval / question card the bot is blocked on (null: unknown, fall back to the last ask). */
  waiting_on?: number | null;
  url?: string;
  title?: string;
  note?: string;
  updated?: number;
  approval?: "ask" | "auto";
  build_access?: "scoped" | "full";
  /** App folders (normalised) whose tools and scripts never raise an approval card for this bot. */
  trusted_apps?: string[];
  /** "super": the one bot per Mac with Claude Code's own tools (docs §5 "Super Bot"); absent for an ordinary bot. */
  kind?: "bot" | "super";
  /** Super Bot's permission posture: ask before writes / edits / shell, or unattended (Claude's own judgement). */
  super_access?: "ask" | "full";
  face?: Face | null;
  routines?: Routine[];
  pinned?: boolean;
  hidden?: boolean;
  /** seq (as a string key) → emoji */
  reactions?: Record<string, string>;
  encrypt?: boolean;
  chrome_profile?: string;
  /** Super Bot only (docs §10; the keys are dropped from every other bot on load): the owner's phone handle, the
   *  phone switch (Settings > Phone), and the people its `text` action may message. */
  imessage?: string;
  imessage_enabled?: boolean;
  imessage_to?: string;
  /** Super Bot only: the tasks it handed to ordinary bots, oldest first (docs §11). */
  handoffs?: Handoff[];
  /** The channel the running task came from; null/absent for a web task. */
  task_via?: Via | null;
  builds?: unknown;
  pending_offer?: { seq: number; [k: string]: unknown } | null;
  offers_declined?: unknown;
  artifacts_dir?: string;
  control?: boolean;
  visible?: boolean;
  dl_pct?: number | null;
  engine?: "auto" | "steps" | "agent";
  seq: number;
  browser?: BrowserState;
  memory?: string | null;
  skills?: Skill[] | null;
  /** "/api/bots/<id>/shot" or null; use shotUrl(b) for a cache-busted src. */
  shot?: string | null;
  shot_ts?: number;
  viewport?: [number, number];
  /** Events past the page's cursor for this bot (merged into the store, then dropped). */
  events: BotEvent[];
}

export interface UsageBotRow { id: string; name: string; today: number; week: number; models: Record<string, number>; errors: number; last: number; live: boolean }
export interface UsageSummary {
  today: number;
  hour: number;
  errors: number;
  origin: { routine: number; manual: number };
  bots: UsageBotRow[];
  tasks: Record<string, number>;
  /** 24 values, oldest → newest. */
  hours: number[];
  days: { day: string; n: number }[];
}

export interface ChannelIdentity { mode: "own" | "dedicated" | ""; label: string }
export interface ImessageState {
  running: boolean; error: string; last_in: number | null; last_out: number | null; handles: number; holder: string; ts?: number;
  /** Who the bot speaks as: "own" = the user's own Messages account (texts carry "@name"), "dedicated" = a bot Apple ID. */
  identity?: ChannelIdentity;
  /** This Mac's own Messages accounts (the `account` of rows it sent), learned once chat.db was read; what
   *  Settings > Phone offers as "use my own number". */
  own_handles?: string[];
  /** GET /api/bots/imessage only: Super Bot's id, its phone switch and its stored handle (kept while off). */
  super_id?: string | null;
  enabled?: boolean;
  handle?: string;
  [k: string]: unknown;
}

export interface StatusReply { bots: Bot[]; ts: number; usage: UsageSummary | null; imessage: ImessageState | null; channels?: Record<string, ImessageState> | null }

export interface AppRow { folder: string; dir: string; name: string; desc: string; tools: unknown; skill: unknown; icon: string | null; mtime: number }
export interface BuildRow { entryId: string; name: string; dir: string; createdAt: number; doneAt?: number }
export interface ChromeProfile { dir: string; name: string; email: string }
/** GET /api/bots/presets: a site the bot knows. `skills` are the playbook titles it comes with. */
export interface Preset { key: string; name: string; color: string; order: number; model: string; instructions: string; apps: string[]; skills: string[] }
/** GET /api/bot-apps/starters: an app that ships with fused-render (FusedBot starters), with its install state under the apps root. */
export interface Starter {
  key: string; name: string; desc: string; version: string; tools: number; icon: string; setup_tool: string; ready_key: string;
  installed: boolean; dir: string; installed_version: string; update: boolean;
}
export interface StarterInstallReply { ok: true; key: string; dir: string; installed: boolean; existed: boolean; name: string }

export interface Ok { ok: true }
/** GET /api/bots/setup. `chrome.found` null = could not tell; `claude` is fused-render's cached Claude Code health
 *  snapshot (never a spawn), read loosely: only `found` and `signed_in` are looked at. */
export interface SetupReply {
  chrome: { found: boolean | null; path: string | null };
  claude: { found?: boolean | null; signed_in?: boolean | null; version?: string | null; [k: string]: unknown };
}

// ------------------------------------------------------------------ instrumentation ----
export const SLOW_MS = 2000, STALL_MS = 8000;
export interface SlowCall { ts: number; action: string; ms: number }
/** Installed by the store: stall shows the banner, settle hides it (only if a stall showed it), slow is logged. */
export const apiHooks: { onStall: (msg: string) => void; onSettle: () => void; onSlow: (rec: SlowCall) => void } = {
  onStall: () => {}, onSettle: () => {}, onSlow: () => {},
};
let inflight = 0, stallTimer: ReturnType<typeof setInterval> | null = null, stallShown = false;

/** Thrown for a non-2xx reply; `message` is the server's `{error}` sentence when it sent one. */
export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) { super(message); this.status = status; }
}

type Method = "GET" | "POST" | "DELETE";
export async function request<T>(method: Method, url: string, body?: unknown, label?: string): Promise<T> {
  const t0 = performance.now(), name = label || url.replace(/^\/api\/(bots\/)?/, "").split("?")[0] || "status";
  inflight++;
  if (!stallTimer) stallTimer = setInterval(() => {
    const s = Math.round((performance.now() - t0) / 1000);
    if (s * 1000 >= STALL_MS) { stallShown = true; apiHooks.onStall(`Still waiting on the worker (${name}, ${s} s)…`); }
  }, 1000);
  try {
    const headers: Record<string, string> = {};
    if (method !== "GET") headers["X-Fused"] = "1";
    if (body !== undefined) headers["Content-Type"] = "application/json";
    const r = await fetch(url, { method, headers, body: body === undefined ? undefined : JSON.stringify(body), cache: "no-store" });
    const text = await r.text();
    let data: unknown = null;
    try { data = text ? JSON.parse(text) : null; } catch { data = null; }
    if (!r.ok) {
      const err = data && typeof data === "object" ? (data as { error?: unknown }).error : undefined;
      const msg = (err ? String(err) : "") || text.trim() || `${r.status} ${r.statusText}`;
      throw new ApiError(msg, r.status);
    }
    return data as T;
  } finally {
    inflight--;
    if (!inflight) { if (stallTimer) clearInterval(stallTimer); stallTimer = null; if (stallShown) { stallShown = false; apiHooks.onSettle(); } }
    const ms = Math.round(performance.now() - t0);
    if (ms > SLOW_MS) { console.warn(`[bots] slow worker call: ${name} took ${ms} ms`); apiHooks.onSlow({ ts: Date.now(), action: name, ms }); }
  }
}

const B = "/api/bots";
const bid = (id: string) => `${B}/${encodeURIComponent(id)}`;
const get = <T>(url: string, label?: string) => request<T>("GET", url, undefined, label);
const post = <T>(url: string, body: unknown = {}, label?: string) => request<T>("POST", url, body, label);

// ------------------------------------------------------------------ routes (§3) ----
/** `preset`: a preset key ("" for none); the backend copies its playbooks and sets its face. */
export interface NewBotBody {
  name: string; model?: string; effort?: string; instructions?: string; approval?: string; build_access?: string; encrypt?: boolean; preset?: string;
  kind?: string; super_access?: string;
}
export interface SettingsBody {
  name?: string; model?: string; effort?: string; instructions?: string; memory?: string; approval?: string;
  build_access?: string; encrypt?: boolean; imessage_handle?: string; imessage_to?: string; imessage_enabled?: boolean;
  super_access?: string; trusted_apps?: string[];
}
export type RoutineBody =
  | { op: "add"; text: string; kind: Routine["kind"]; minutes?: number; time?: string; weekdays?: number[]; at?: number }
  | { op: "delete" | "enable" | "disable" | "run"; rid: string };
export type SkillBody =
  | { op: "save"; name: string; trigger: string; text: string; rid?: string }
  | { op: "delete"; rid: string }
  | { op: "learn" };

export const api = {
  /** GET /api/bots — the poll. */
  status: (p: { cursors: Record<string, number>; shot_for: string; fast: boolean }) =>
    get<StatusReply>(`${B}?cursors=${encodeURIComponent(JSON.stringify(p.cursors))}&shot_for=${encodeURIComponent(p.shot_for)}&fast=${p.fast ? 1 : 0}`, "status"),
  create: (body: NewBotBody) => post<{ ok: true; id: string }>(B, body, "create"),
  profiles: () => get<{ ok: true; profiles: ChromeProfile[] }>(`${B}/profiles`, "profiles"),
  presets: () => get<{ ok: true; presets: Preset[] }>(`${B}/presets`, "presets"),
  usage: () => get<UsageSummary>(`${B}/usage`, "usage"),
  imessage: () => get<ImessageState>(`${B}/imessage`, "imessage"),
  /** Settings > Phone "Send a test text": one text from this Mac to Super Bot's handle (the Automation consent lands here). */
  imessageTest: () => post<{ ok: true; handle: string }>(`${B}/imessage/test`, {}, "test text"),
  /** Also answers approvals ("approve"/"deny"), questions and offers. */
  send: (id: string, text: string, reply_to?: number | null) => post<Ok>(`${bid(id)}/send`, { text, reply_to: reply_to ?? null }, "send"),
  pause: (id: string) => post<Ok>(`${bid(id)}/pause`, {}, "pause"),
  resume: (id: string) => post<Ok>(`${bid(id)}/resume`, {}, "resume"),
  stop: (id: string) => post<Ok>(`${bid(id)}/stop`, {}, "stop"),
  takeover: (id: string) => post<Ok>(`${bid(id)}/takeover`, {}, "takeover"),
  giveback: (id: string) => post<Ok>(`${bid(id)}/giveback`, {}, "giveback"),
  wake: (id: string) => post<Ok>(`${bid(id)}/wake`, {}, "wake"),
  window: (id: string, visible: boolean) => post<Ok>(`${bid(id)}/window`, { visible }, "window"),
  goto: (id: string, url: string) => post<{ ok: true; url: string }>(`${bid(id)}/goto`, { url }, "goto"),
  nav: (id: string, op: "back" | "forward" | "reload") => post<{ ok: true; url: string }>(`${bid(id)}/nav`, { op }, "nav"),
  tab: (id: string, body: { tab: "new" | "switch" | "close"; url?: string; index?: number }) =>
    post<{ ok: true; url: string; tabs: Tab[] }>(`${bid(id)}/tab`, body, "tab"),
  /** data: base64 (no data: prefix); 8 MB cap server-side. */
  attach: (id: string, name: string, data: string) => post<{ ok: true; name: string }>(`${bid(id)}/attach`, { name, data }, "attach"),
  react: (id: string, seq: number, emoji: string) => post<{ ok: true; reactions: Record<string, string> }>(`${bid(id)}/react`, { seq, emoji }, "react"),
  flag: (id: string, body: { pinned?: boolean; hidden?: boolean; face?: Face }) => post<Ok>(`${bid(id)}/flag`, body, "flag"),
  settings: (id: string, body: SettingsBody) => post<Ok>(`${bid(id)}/settings`, body, "settings"),
  profile: (id: string, profile: string) => post<Ok>(`${bid(id)}/profile`, { profile }, "profile"),
  clone: (id: string, name?: string) => post<{ ok: true; id: string }>(`${bid(id)}/clone`, name ? { name } : {}, "clone"),
  remove: (id: string) => request<Ok>("DELETE", bid(id), undefined, "delete"),
  routines: (id: string, body: RoutineBody) => post<{ ok: true; routine?: Routine }>(`${bid(id)}/routines`, body, "routine"),
  skills: (id: string, body: SkillBody) => post<{ ok: true; skills: Skill[] }>(`${bid(id)}/skills`, body, "skill"),
  exportTranscript: (id: string) => get<{ ok: true; name: string; text: string }>(`${bid(id)}/export`, "export"),
  reveal: (id: string, path?: string) => post<{ ok: true; path: string }>(`${bid(id)}/reveal`, path ? { path } : {}, "reveal"),
  builds: () => get<{ builds: BuildRow[] }>(`${B}/builds`, "builds"),
  saveBuilds: (builds: BuildRow[]) => post<Ok>(`${B}/builds`, { builds }, "builds"),
  apps: () => get<{ root: string; apps: AppRow[] }>("/api/bot-apps", "apps"),
  importApp: (name: string, data: string) => post<{ dir: string; folder: string; files: number; fusedApp: boolean }>("/api/bot-apps/import", { name, data }, "importapp"),
  mkdirApp: (dir: string) => post<{ dir: string; existed: boolean }>("/api/bot-apps/mkdir", { dir }, "mkbuild"),
  revealApp: (dir: string) => post<{ dir: string }>("/api/bot-apps/reveal", { dir }, "revealapp"),
  starters: () => get<{ root: string; starters: Starter[] }>("/api/bot-apps/starters", "starters"),
  starterInstall: (key: string) => post<StarterInstallReply>(`/api/bot-apps/starters/${encodeURIComponent(key)}/install`, {}, "starter_install"),
  starterUpdate: (key: string) => post<StarterInstallReply>(`/api/bot-apps/starters/${encodeURIComponent(key)}/update`, {}, "starter_update"),
  /** GET /api/bots/setup: is there a Chrome to drive and a Claude Code to think with (the empty hero's setup lines). */
  setup: () => get<SetupReply>(`${B}/setup`, "setup"),
  /** Slow (runs each installed starter's setup tool): call after the strip is drawn. null = could not tell. */
  starterStatus: () => get<{ ok: true; ready: Record<string, boolean | null>; why: Record<string, string> }>("/api/bot-apps/starters/status", "starter_status"),
};

// ------------------------------------------------------------------ URL builders ----
/** The preview <img> src: the shot route, cache-busted by shot_ts (OpenBot shotUrl). Empty when the bot has no shot. */
export const shotUrl = (b: Pick<Bot, "shot" | "shot_ts">): string =>
  b.shot ? b.shot + (b.shot.includes("?") ? "&" : "?") + "t=" + Math.round((b.shot_ts || 0) * 1000) : "";
/** A step thumbnail ("<seq>.jpg") → its route. */
export const stepThumbUrl = (botId: string, thumb: string): string =>
  `${bid(botId)}/steps/${encodeURIComponent(thumb.replace(/^.*\//, ""))}`;
/** A file on disk → its raw bytes (/api/fs/raw): the ↓ download links and the Composer reading a save result back. */
export const rawFileUrl = (path: string): string => "/api/fs/raw?path=" + encodeURIComponent(path);
/** An app folder's icon (404 when it has none). */
export const appIconUrl = (dir: string): string => "/api/bot-apps/icon?dir=" + encodeURIComponent(dir);
/** A starter app's icon (OpenBot's rawUrl("starters/<key>/<icon>")). */
export const starterIconUrl = (key: string): string => `/api/bot-apps/starters/${encodeURIComponent(key)}/icon`;
