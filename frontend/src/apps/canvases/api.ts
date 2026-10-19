// Typed bridge to fused_render/canvases.py — same transport as the shared
// helpers (thrown HttpError, X-Fused write guard). The guarded GETs (list,
// whoami, token) execute a CLI child / hand out a credential, so they carry
// the X-Fused header too (the server 403s them without it).
import { getJson, postJson } from "@platform/lib/api";
import { ownerFromError, type SyncLogEvent, type SyncOwner, type SyncState } from "./sync-state";

export type { SyncLeg, SyncLogEvent, SyncOwner, SyncState } from "./sync-state";

const GUARD = { headers: { "X-Fused": "1" } };

export interface CanvasesStatus {
  cli_found: boolean;
  logged_in: boolean;
  /** Credentials-file mtime, or null — changes when a (re-)login completes. */
  creds_stamp: number | null;
  login_in_flight: boolean;
  workbench_base_url: string;
  /** Which Fused environment canvases, the `fused` wrapper and share uploads
   *  target (fusedcli.workbench_env): `unstable` or `prod`, or whatever
   *  FUSED_RENDER_WORKBENCH_ENV pins. */
  workbench_env: string;
  canvases_dir: string;
  /** Clones on this machine — what a switch of environment deletes. */
  cloned_count: number;
}

export type WorkbenchEnv = "unstable" | "prod";

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

export const getCanvasesStatus = () => getJson<CanvasesStatus>("/api/canvases/status");

export const getWhoami = () =>
  getJson<{ handle: string | null }>("/api/canvases/whoami", GUARD);

export const startLogin = () => postJson<{ ok: boolean }>("/api/canvases/login", {});

export const cancelLogin = () =>
  postJson<{ ok: boolean }>("/api/canvases/login/cancel", {});

export const listCanvases = () =>
  getJson<{ canvases: CanvasEntry[] }>("/api/canvases/list", GUARD);

/** Presigned preview URLs for the given collection ids, signed in parallel
 *  server-side. A missing or null entry just means "no preview" — the card
 *  keeps its letter thumb. */
export const getCanvasPreviews = (ids: string[]) =>
  postJson<{ previews: Record<string, string | null> }>("/api/canvases/previews", {
    ids,
  });

export const createCanvas = (name: string) =>
  postJson<{ ok: boolean; name: string }>("/api/canvases/create", { name });

export const logout = () => postJson<{ ok: boolean }>("/api/canvases/logout", {});
/** Switch the Fused environment. DELETES every cloned canvas first (the one
 *  writer of the `workbench_env` preference); 409 while another process holds
 *  a clone, 423 while the env var pins the environment. */
export const switchWorkbenchEnv = (env: WorkbenchEnv, expectedCloned: number) =>
  postJson<CanvasesStatus>("/api/canvases/env", { env, expected_cloned: expectedCloned });

/** The body of a switch 409 that means "the clone count moved since the
 *  page read it" — the page re-reads and asks again. */
export interface ClonesChanged {
  code: "clones_changed";
  cloned_count: number;
}
export function clonesChanged(e: unknown): ClonesChanged | null {
  const body = (e as { status?: number; body?: unknown }).body as Partial<ClonesChanged> | null;
  if ((e as { status?: number }).status !== 409 || body?.code !== "clones_changed") return null;
  return typeof body.cloned_count === "number" ? (body as ClonesChanged) : null;
}

export const cloneCanvas = (name: string) =>
  postJson<{ ok: boolean; dir: string }>("/api/canvases/clone", { name });

/** Embed v3: one workbench snapshot → the clone's canvas.toml
 *  (canvas_snapshot.py). Throws with `.status` 409 while a sync leg holds
 *  the clone, 400 when the snapshot is rejected. */
export const applySnapshot = (
  name: string,
  gen: number,
  seq: number,
  body: Record<string, unknown>,
) =>
  postJson<{ ok: boolean; gen: number; seq: number; changed: boolean; push_seq: number }>(
    "/api/canvases/snapshot",
    { name, gen, seq, body },
  );

/** Embed v3: the workbench's rename hint, same endpoint, same ack shape. */
export const applyRename = (
  name: string,
  gen: number,
  seq: number,
  rename: { from: string; to: string; file_from?: string | null; file_to?: string | null },
) =>
  postJson<{ ok: boolean; gen: number; seq: number; changed: boolean; push_seq: number }>(
    "/api/canvases/snapshot",
    { name, gen, seq, rename },
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
export const getDelta = (name: string, full: boolean) =>
  getJson<CanvasDelta>(
    `/api/canvases/delta?name=${encodeURIComponent(name)}&full=${full ? 1 : 0}`,
  );

/** The frame applied (`ok`) or refused a delta: the host's picture of the
 *  frame moves to that revision, or is dropped so the next delta is full. */
export const ackDelta = (name: string, rev: number, ok: boolean, reason?: string) =>
  postJson<{ ok: boolean; rev: number }>("/api/canvases/delta/ack", {
    name,
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
export const pullCanvas = (name: string, hard = false) =>
  postJson<SyncStatus & { ok: boolean; error?: string | null }>(
    "/api/canvases/sync/pull",
    hard ? { name, hard: true } : { name },
  );

/** Ask the watcher to probe the server for changes now (the window or tab
 *  just came into focus) instead of on its next timed poll. Detect-only;
 *  a stale verdict arrives as the usual sync state (the Pull button). */
export const checkRemote = (name: string) =>
  postJson<{ ok: boolean; watching: boolean }>("/api/canvases/sync/check", { name });

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
export const startSync = async (name: string): Promise<SyncStatus> => {
  try {
    return await postJson<SyncStatus>("/api/canvases/sync/start", { name });
  } catch (e) {
    const owner = ownerFromError(e);
    if (owner) throw new CanvasOwnedError(owner, (e as { body?: unknown }).body, (e as Error).message);
    throw e;
  }
};

/** The four sync legs (embed_to_disk is the page's own: it fills that leg
 *  from its relay queue), disk_rev / watcher_epoch, and the lock owner. */
export const getSyncState = (name: string) =>
  getJson<SyncState>(`/api/canvases/sync/state?name=${encodeURIComponent(name)}`);

/** The sync log ring (200 per canvas) after `after` (a seq; 0 = all held).
 *  `next` is the seq to pass on the following call. */
export const getSyncLog = (name: string, after: number) =>
  getJson<{ events: SyncLogEvent[]; next: number }>(
    `/api/canvases/sync/log?name=${encodeURIComponent(name)}&after=${after}`,
  );

/** Same-origin URL of WS /api/canvases/sync/events (state / log / ping). */
export const syncEventsUrl = (name: string) =>
  (location.protocol === "https:" ? "wss://" : "ws://") +
  location.host +
  `/api/canvases/sync/events?name=${encodeURIComponent(name)}`;

/** Spawn a Claude session on the canvas clone primed with the failing
 *  push's errors; attach the chat iframe with the returned run_id. */
export const fixWithClaude = (name: string) =>
  postJson<{ ok: boolean; run_id: string }>("/api/canvases/fix", { name });

export const getAccessToken = () =>
  getJson<{ access_token: string }>("/api/canvases/token", GUARD);
