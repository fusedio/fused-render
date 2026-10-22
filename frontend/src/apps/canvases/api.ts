// Typed bridge to fused_render/canvases.py — same transport as the shared
// helpers (thrown HttpError, X-Fused write guard). The guarded GETs (list,
// whoami, token) execute a CLI child / hand out a credential, so they carry
// the X-Fused header too (the server 403s them without it).
//
// Every canvas-scoped call names its Fused environment: a canvas's identity is
// (env, name), because a prod canvas and an unstable canvas can be open and
// syncing at the same time — the clone lives at ~/Fused/canvas/<env>/<name>
// and its watcher, CLI runs and embed all target that env. The `workbench_env`
// preference is only the DEFAULT (the catalog's initial pick); nothing here
// reads it. login / logout / token / status take an OPTIONAL env: prod and
// unstable share one `fused login` store, but custom has its own `[auth]`
// (its deployment's tenant), so a caller about one canvas names its env and
// one that means "this machine's login" (Settings, Share, sidebar) omits it.
import { getJson, postJson } from "@platform/lib/api";
import { ownerFromError, type SyncLogEvent, type SyncOwner, type SyncState } from "./sync-state";

export type { SyncLeg, SyncLogEvent, SyncOwner, SyncState } from "./sync-state";

const GUARD = { headers: { "X-Fused": "1" } };

export interface CanvasesStatus {
  cli_found: boolean;
  /** `auth_env`'s `fused login` store exists. */
  logged_in: boolean;
  /** Which env's login `logged_in` / `creds_stamp` describe: the `env` the
   *  status was asked about, else the default env. */
  auth_env: string;
  /** Credentials-file mtime, or null — changes when a (re-)login completes. */
  creds_stamp: number | null;
  login_in_flight: boolean;
  /** The DEFAULT environment (fusedcli.workbench_env): the catalog's initial
   *  pick, the `fused` wrapper's env outside a clone, share uploads' target.
   *  Canvases keep the env they were cloned from regardless. A string, not a
   *  WorkbenchEnv: FUSED_RENDER_WORKBENCH_ENV may pin a stg/dev/PR env outside
   *  the choices, so readers narrow it with isWorkbenchEnv. */
  workbench_env: string;
  /** FUSED_RENDER_WORKBENCH_ENV pins the environment: the server answers 423
   *  to any other env (a dev server points unstable at localhost, so a prod
   *  tab from there would hit real prod), and the catalog disables its
   *  environment dropdown. */
  locked: boolean;
  /** Per environment: its workbench web origin, the origin the workspace
   *  frames (the same, or — custom env with the embed proxy on — the
   *  loopback proxy in front of it), its clone folder
   *  (`~/Fused/canvas/<env>`), how many clones it holds here, and whether it
   *  is configured (false only for custom whose `~/.fused/settings.toml`
   *  names no `base_url`). The Settings picker disables an unconfigured
   *  custom; the Canvases page offers it and opens the settings editor. */
  envs: Record<
    WorkbenchEnv,
    {
      base_url: string;
      embed_url?: string;
      canvases_dir: string;
      cloned_count: number;
      available: boolean;
    }
  >;
  /** The custom environment's settings file (`~/.fused/settings.toml`,
   *  expanded) — what the settings editor edits. */
  settings_path: string;
}

/** The settings editor's read. Creates the file EMPTY when there is none,
 *  so the reader edits a real path from the start; an empty file configures
 *  nothing, so `configured` stays false until a `base_url` is saved. */
export const openFusedSettings = () =>
  postJson<{ path: string; text: string; created: boolean; configured: boolean }>(
    "/api/canvases/settings/open",
    {},
  );

/** Write the settings file. A paste that is not TOML is refused (400,
 *  code `invalid_toml`, the parser's sentence in `error`) and the file is
 *  left as it was. */
export const saveFusedSettings = (text: string) =>
  postJson<{
    ok: boolean;
    path: string;
    configured: boolean;
    base_url: string | null;
    web_url: string;
  }>("/api/canvases/settings/save", { text });

/** Remove the settings file (only the TOML one); custom is unconfigured
 *  again afterwards. A missing file is `removed: false`, not an error. */
export const deleteFusedSettings = () =>
  postJson<{ ok: boolean; path: string; removed: boolean; configured: boolean }>(
    "/api/canvases/settings/delete",
    {},
  );

/** `custom` (fusedcli.CUSTOM_WORKBENCH_ENV) is no Fused of its own: its CLI
 *  runs leave FUSED_ENV unset, so they talk to whatever endpoint
 *  `~/.fused/settings.toml` names, and its `base_url` is that endpoint's web
 *  origin. */
export type WorkbenchEnv = "unstable" | "prod" | "custom";

/** fusedcli.WORKBENCH_ENV_CHOICES, in the server's order. */
export const WORKBENCH_ENVS: readonly WorkbenchEnv[] = ["unstable", "prod", "custom"];

export const isWorkbenchEnv = (s: unknown): s is WorkbenchEnv =>
  typeof s === "string" && (WORKBENCH_ENVS as readonly string[]).includes(s);

export const ENV_LABELS: Record<WorkbenchEnv, string> = {
  unstable: "Unstable",
  prod: "Production",
  custom: "Custom ~/.fused/settings.toml",
};

/** The label where room is tight (a sidebar row's tag): custom drops its path. */
export const ENV_TAGS: Record<WorkbenchEnv, string> = { ...ENV_LABELS, custom: "Custom" };

/** A canvas workspace route: the env is a path segment because it is one on
 *  disk (~/Fused/canvas/<env>/<name>). The one-segment `/canvases/<name>` is
 *  deliberately unrecognised (a 404, not a redirect: which env it meant is
 *  unknowable once both can be open). */
export const canvasWorkspacePath = (env: WorkbenchEnv, name: string) =>
  `/canvases/${env}/${encodeURIComponent(name)}`;

/** `/canvases/<env>/<name>` → {env, name}, else null — App.tsx's route and
 *  the inverse of canvasWorkspacePath. The name alphabet is the CLI's own, so
 *  the match is also the validation; the one-segment form never matches. */
export function canvasNameFromPath(
  pathname: string,
): { env: WorkbenchEnv; name: string } | null {
  const match = /^\/canvases\/([a-z]+)\/([A-Za-z0-9_]+)$/.exec(pathname);
  if (!match || !isWorkbenchEnv(match[1])) return null;
  return { env: match[1], name: match[2] };
}

/** The catalog listing `env`. The dropdown's state lives in the URL query —
 *  browser storage holds no facts (D1386) — so a back link lands on the
 *  environment the canvas came from. */
export const catalogPath = (env: WorkbenchEnv) => `/canvases?env=${env}`;

/** The canvas PROJECT page's tabs (`/canvases-project/<env>/<name>`), strip
 *  order; the first is the default. THE list: the type is derived from it and
 *  CanvasPage.tsx's `TAB_DEFS` is a Record over that type, so a tab added
 *  here and forgotten there is a type error (the app page's APP_PAGE_TABS
 *  pattern, current-apps-lib.ts). Overview is the workbench framed read-only,
 *  Tasks the Tasks page scoped to the clone, Files the clone's files in their
 *  templates, Sync the clone's sync panel (the strip, the log, Pull / Hard
 *  pull, a failed push's detail) — the Overview shows the canvas and nothing
 *  else (owner, 2026-10-20). No API or Doctor tab: a canvas is not an app
 *  folder with endpoints to call or a share-readiness to check. */
export const CANVAS_PAGE_TABS = ["overview", "tasks", "files", "sync"] as const;
export type CanvasPageTab = (typeof CANVAS_PAGE_TABS)[number];
export const DEFAULT_CANVAS_PAGE_TAB: CanvasPageTab = CANVAS_PAGE_TABS[0];
/** The query param naming the tab; absent = the default (the app page's `_tab`). */
export const CANVAS_PAGE_TAB_PARAM = "_tab";

/** The tab a query names. Missing or unknown falls back to the default
 *  silently: a stale link opens the page, not an error. */
export function canvasPageTabFromSearch(search: string): CanvasPageTab {
  const tab = new URLSearchParams(search).get(CANVAS_PAGE_TAB_PARAM);
  return tab !== null && (CANVAS_PAGE_TABS as readonly string[]).includes(tab)
    ? (tab as CanvasPageTab)
    : DEFAULT_CANVAS_PAGE_TAB;
}

/** The canvas EDITOR page (`/canvases/<env>/<name>`: the workbench beside
 *  the Claude chat, sync, the edit workflow), optionally on one NODE with
 *  one CHAT open: `?node=` + `node_file=` select the node (what
 *  CanvasWorkspace's `nodeFromUrl` reads; the stem is the UDF name, exactly
 *  as its `nodeHopFromUrl` derives it) and `session_id` is the conversation
 *  the right pane boots on — the node's chat when a node is named, the
 *  canvas chat otherwise (both read it off this page's URL). The Tasks
 *  page's door on a canvas task is built here (tasks-lib `taskHref`), so the
 *  page's own query grammar has one author. */
export function canvasPagePath(
  env: WorkbenchEnv,
  name: string,
  opts?: { file?: string; sessionId?: string },
): string {
  const q = new URLSearchParams();
  const file = opts?.file || "";
  if (file) {
    const dot = file.lastIndexOf(".");
    q.set("node", dot > 0 ? file.slice(0, dot) : file);
    q.set("node_file", file);
  }
  if (opts?.sessionId) q.set("session_id", opts.sessionId);
  const search = q.toString();
  return `/canvases/${env}/${name}` + (search ? "?" + search : "");
}

/** The canvas PROJECT page (`/canvases-project/<env>/<name>?_tab=`, owner
 *  2026-10-20): the tabbed, read-only view of one canvas — for visibility,
 *  not editing. A SEPARATE route from the editor above on purpose: the
 *  editor's two-pane shape, its sync strip and its edit workflow are the
 *  owner's and must not be folded into tabs (the first cut did exactly that
 *  and was reversed). `search`: the current query to carry across a TAB hop
 *  (a tab's own params — `?view=` on Tasks, `?file=` on Files — ride along
 *  untouched, the app page's rule); only `_tab` is rewritten, and dropped
 *  for the default so the default tab has one address. */
export function canvasProjectPath(
  env: WorkbenchEnv,
  name: string,
  opts?: { tab?: CanvasPageTab; search?: string },
): string {
  const q = new URLSearchParams(opts?.search ?? "");
  if (opts?.tab !== undefined) {
    if (opts.tab === DEFAULT_CANVAS_PAGE_TAB) q.delete(CANVAS_PAGE_TAB_PARAM);
    else q.set(CANVAS_PAGE_TAB_PARAM, opts.tab);
  }
  const search = q.toString();
  return `/canvases-project/${env}/${name}` + (search ? "?" + search : "");
}

/** `/canvases-project/<env>/<name>` → the canvas, or null (same alphabet as
 *  `canvasNameFromPath`). */
export function canvasProjectFromPath(
  pathname: string,
): { env: WorkbenchEnv; name: string } | null {
  const match = /^\/canvases-project\/([a-z]+)\/([A-Za-z0-9_]+)$/.exec(pathname);
  if (!match || !isWorkbenchEnv(match[1])) return null;
  return { env: match[1], name: match[2] };
}

const q = (env: WorkbenchEnv, name: string) =>
  `name=${encodeURIComponent(name)}&env=${env}`;

export interface CanvasEntry {
  name: string;
  id: string | null;
  cloned: boolean;
  /** *.py files in the local clone; null when not cloned. */
  n_udfs: number | null;
  /** Newest file mtime (epoch seconds) in the local clone; null when not cloned. */
  mtime: number | null;
  /** The workbench's own code-UDF count (nodes minus sticky notes, widgets and
   *  apps), for every canvas in the account whether cloned or not — the lite
   *  listing already carries the node list, so it costs no round trip. Null
   *  only on the external-CLI fallback path, which lists bare names. */
  n_code_udfs?: number | null;
  /** Canvas preview image URL, when it costs nothing to know: a public https
   *  URL already in the list payload. A preview held in the private image
   *  bucket arrives null here with `preview_pending` set — signing it is a
   *  control-plane round trip per canvas, kept off the listing's critical path
   *  (D364) and fetched by getCanvasPreviews once the cards are on screen. */
  preview_url: string | null;
  /** This canvas has an uploaded preview whose URL still needs signing. Older
   *  servers omit the field, so treat a missing value as false. */
  preview_pending?: boolean;
  /** Control-plane last_updated (epoch seconds); null on the external-CLI fallback. */
  updated_at: number | null;
}

/** The watcher's status as sync/start and sync/pull answer it, cut to what
 *  this page reads: the clone dir (start) and a pull's verdict (pull). The
 *  live picture — legs, disk_rev, fix_active — is sync/state (SyncState). */
export interface SyncStatus {
  name: string;
  dir: string;
  /** Set on a pull answer that failed (`ok: false`). */
  pull_error?: string | null;
  /** The last push's error, if any. */
  error: string | null;
}

/** `logged_in` / `creds_stamp` describe `env`'s login when given (else the
 *  default env's); `auth_env` says which. */
export const getCanvasesStatus = (env?: WorkbenchEnv | null) =>
  getJson<CanvasesStatus>(env ? `/api/canvases/status?env=${env}` : "/api/canvases/status");

/** The account handle. Handles are per environment: the canvas workspace
 *  names its canvas's env (the handle goes into the workbench URL); a caller
 *  that only needs "who is signed in" (Settings, Share) omits it and the
 *  server answers in the auth env. */
export const getWhoami = (env?: WorkbenchEnv) =>
  getJson<{ handle: string | null }>(
    env ? `/api/canvases/whoami?env=${env}` : "/api/canvases/whoami",
    GUARD,
  );

export const startLogin = (env?: WorkbenchEnv | null) =>
  postJson<{ ok: boolean }>(env ? `/api/canvases/login?env=${env}` : "/api/canvases/login", {});

export const cancelLogin = () =>
  postJson<{ ok: boolean }>("/api/canvases/login/cancel", {});

/** The server kills the `fused login` child at this mark
 *  (canvases.LOGIN_CHILD_TIMEOUT); status then reads `login_in_flight: false`
 *  like any other early end, so the page tells the two apart by the clock. */
export const LOGIN_TIMEOUT_MS = 5 * 60_000;

/** What to tell the reader when a sign-in they started ended without the
 *  credentials changing: the deadline, or the child quitting early (consent
 *  declined, tab closed). `startedAt` is when the page started it, or null. */
export function loginOutcome(startedAt: number | null, now = Date.now()): string {
  // A few seconds short of the mark: the poll sees the death up to a tick late,
  // never early, so anything this close is the watchdog.
  if (startedAt !== null && now - startedAt >= LOGIN_TIMEOUT_MS - 5_000) {
    return "Sign-in timed out after 5 minutes — try again.";
  }
  return "Sign-in was not completed — try again.";
}

export const listCanvases = (env: WorkbenchEnv) =>
  getJson<{ canvases: CanvasEntry[] }>(`/api/canvases/list?env=${env}`, GUARD);

/** Presigned preview URLs for the given collection ids, signed in parallel
 *  server-side. A missing or null entry just means "no preview" — the card
 *  keeps its letter thumb. */
export const getCanvasPreviews = (env: WorkbenchEnv, ids: string[]) =>
  postJson<{ previews: Record<string, string | null> }>("/api/canvases/previews", {
    ids,
    env,
  });

export const createCanvas = (env: WorkbenchEnv, name: string) =>
  postJson<{ ok: boolean; name: string }>("/api/canvases/create", { name, env });

export const logout = (env?: WorkbenchEnv | null) =>
  postJson<{ ok: boolean }>(env ? `/api/canvases/logout?env=${env}` : "/api/canvases/logout", {});

export const cloneCanvas = (env: WorkbenchEnv, name: string) =>
  postJson<{ ok: boolean; dir: string }>("/api/canvases/clone", { name, env });

/** Embed v3: one workbench snapshot → the clone's canvas.toml
 *  (canvas_snapshot.py). Throws with `.status` 409 while a sync leg holds
 *  the clone, 400 when the snapshot is rejected. */
export const applySnapshot = (
  env: WorkbenchEnv,
  name: string,
  gen: number,
  seq: number,
  body: Record<string, unknown>,
) =>
  postJson<{ ok: boolean; gen: number; seq: number; changed: boolean; push_seq: number }>(
    "/api/canvases/snapshot",
    { name, env, gen, seq, body },
  );

/** Embed v3: the workbench's rename hint, same endpoint, same ack shape. */
export const applyRename = (
  env: WorkbenchEnv,
  name: string,
  gen: number,
  seq: number,
  rename: { from: string; to: string; file_from?: string | null; file_to?: string | null },
) =>
  postJson<{ ok: boolean; gen: number; seq: number; changed: boolean; push_seq: number }>(
    "/api/canvases/snapshot",
    { name, env, gen, seq, rename },
  );

/** One op of a canvas delta (canvas_snapshot.diff_views): the frame applies
 *  them in order. Opaque to this page beyond `op`. */
export interface DeltaOp {
  op: "upsert" | "rename" | "delete" | "edges" | "canvas";
  [key: string]: unknown;
}

export interface CanvasDelta {
  /** The disk revision this delta brings the frame to. */
  rev: number;
  /** The revision the frame is assumed to hold; null for a full state. */
  base_rev?: number | null;
  ops?: DeltaOp[];
  full?: boolean;
  /** Set (instead of ops) while the clone is mid-write; ask again shortly. */
  held?: string;
}

/** Embed v3: diff(disk, what the frame holds), for `fused-embed-delta`.
 *  `full` asks for the whole state (the frame just hydrated). Throws with
 *  `.status` 409 while a sync leg holds the clone. */
export const getDelta = (env: WorkbenchEnv, name: string, full: boolean) =>
  getJson<CanvasDelta>(`/api/canvases/delta?${q(env, name)}&full=${full ? 1 : 0}`);

/** The frame applied (`ok`) or refused a delta: the host's picture of the
 *  frame moves to that revision, or is dropped so the next delta is full. */
export const ackDelta = (
  env: WorkbenchEnv,
  name: string,
  rev: number,
  ok: boolean,
  reason?: string,
) =>
  postJson<{ ok: boolean; rev: number }>("/api/canvases/delta/ack", {
    name,
    env,
    rev,
    ok,
    // The frame's own words for a refusal ("not hydrated yet" during boot),
    // so the server can tell a frame still booting from a real refusal.
    ...(reason !== undefined ? { reason } : {}),
  });

/** Pull the server's canvas into the clone now (the "Server changes · Pull"
 *  chip). Clean clone: force pull; dirty: three-way merge, canvas.toml per
 *  node, local edits kept. Answers the status with `ok`; the page reads only
 *  the verdict (the new state follows on sync/events). `hard` (the sync
 *  drawer's Hard pull) skips the merge: the clone's local files are
 *  discarded (kept in the canvas trash) and the server's copy taken whole. */
export const pullCanvas = (env: WorkbenchEnv, name: string, hard = false) =>
  postJson<SyncStatus & { ok: boolean; error?: string | null }>(
    "/api/canvases/sync/pull",
    hard ? { name, env, hard: true } : { name, env },
  );

/** Ask the watcher to probe the server for changes now (the window or tab
 *  just came into focus) instead of on its next timed poll. Detect-only;
 *  a stale verdict arrives as the usual sync state (the Pull button). */
export const checkRemote = (env: WorkbenchEnv, name: string) =>
  postJson<{ ok: boolean; watching: boolean }>("/api/canvases/sync/check", { name, env });

/** Thrown by `startSync` when another fused-render process holds this
 *  clone's `.sync/<name>.lock` (409 `owned`). Keeps HttpError's `status` and
 *  `body` for callers that only read those. */
export class CanvasOwnedError extends Error {
  status = 409;
  owner: SyncOwner;
  body: unknown;
  constructor(owner: SyncOwner, body: unknown, message: string) {
    super(message);
    this.name = "CanvasOwnedError";
    this.owner = owner;
    this.body = body;
  }
}

/** Arm the clone watcher. A 409 `owned` (another process is this clone's one
 *  writer) is rethrown as `CanvasOwnedError` carrying `{pid, port, since}`;
 *  any other failure is the plain HttpError. */
export const startSync = async (env: WorkbenchEnv, name: string): Promise<SyncStatus> => {
  try {
    return await postJson<SyncStatus>("/api/canvases/sync/start", { name, env });
  } catch (e) {
    const owner = ownerFromError(e);
    if (owner) throw new CanvasOwnedError(owner, (e as { body?: unknown }).body, (e as Error).message);
    throw e;
  }
};

/** The four sync legs (embed_to_disk is the page's own: it fills that leg
 *  from its relay queue), disk_rev / watcher_epoch, and the lock owner. */
export const getSyncState = (env: WorkbenchEnv, name: string) =>
  getJson<SyncState>(`/api/canvases/sync/state?${q(env, name)}`);

/** The sync log ring (200 per canvas) after `after` (a seq; 0 = all held).
 *  `next` is the seq to pass on the following call. */
export const getSyncLog = (env: WorkbenchEnv, name: string, after: number) =>
  getJson<{ events: SyncLogEvent[]; next: number }>(
    `/api/canvases/sync/log?${q(env, name)}&after=${after}`,
  );

/** Same-origin URL of WS /api/canvases/sync/events (state / log / ping). */
export const syncEventsUrl = (env: WorkbenchEnv, name: string) =>
  (location.protocol === "https:" ? "wss://" : "ws://") +
  location.host +
  `/api/canvases/sync/events?${q(env, name)}`;

/** Spawn a Claude session on the canvas clone primed with the failing
 *  push's errors; attach the chat iframe with the returned run_id. */
export const fixWithClaude = (env: WorkbenchEnv, name: string) =>
  postJson<{ ok: boolean; run_id: string }>("/api/canvases/fix", { name, env });

/** The token for `env`'s workbench — the CANVAS's env, so a custom canvas
 *  gets its own deployment's login, never the hosted one. */
export const getAccessToken = (env: WorkbenchEnv) =>
  getJson<{ access_token: string }>(`/api/canvases/token?env=${env}`, GUARD);
