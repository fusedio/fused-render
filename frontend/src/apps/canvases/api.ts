// Typed bridge to fused_render/canvases.py — same transport as the shared
// helpers (thrown HttpError, X-Fused write guard). The guarded GETs (list,
// whoami, token) execute a CLI child / hand out a credential, so they carry
// the X-Fused header too (the server 403s them without it).
import { getJson, postJson } from "@platform/lib/api";

const GUARD = { headers: { "X-Fused": "1" } };

export interface CanvasesStatus {
  cli_found: boolean;
  logged_in: boolean;
  /** Credentials-file mtime, or null — changes when a (re-)login completes. */
  creds_stamp: number | null;
  login_in_flight: boolean;
  workbench_base_url: string;
  canvases_dir: string;
}

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

export interface SyncStatus {
  name: string;
  dir: string;
  watching: boolean;
  /** "held": the clone has unpushed edits but the server moved since the
   *  last sync (see `server_stale`); nothing is pushed until the user pulls. */
  push_state: "idle" | "pending" | "pushing" | "error" | "held";
  push_seq: number;
  last_push_at: number | null;
  /** The clone's disk revision: moves whenever the folder's fingerprint moves
   *  (a Claude edit, a merge, an embed snapshot landing). The workspace asks
   *  for a delta (getDelta) when it advances. Older servers omit it. */
  disk_rev?: number;
  /** The canvas changed on the server since the last sync point and has not
   *  been pulled: what a hosted workbench tab shows as "Server changes". The
   *  watcher stops pushing while this is set; `pullCanvas` clears it. */
  server_stale?: {
    since: number;
    udfs: { slug: string; kind: "added" | "changed" | "removed" }[];
    layout: boolean;
  } | null;
  /** Increments when the watcher pulls remote (workbench-side) changes down. */
  pull_seq: number;
  last_pull_at: number | null;
  error: string | null;
  /** Full per-line CLI output of the failing push (e.g. one validation
   *  error per line); empty when the last push succeeded. */
  error_detail: string[];
  /** A "Fix with Claude" session is running on this clone right now — set the
   *  instant one spawns, cleared only by that run's own completion (never a
   *  transcript-activity guess), so it's safe to gate a second spawn on. */
  fix_active: boolean;
  /** A Claude session is live in this clone right now (a live claude
   *  process, never a transcript-activity guess). Informational only — the
   *  left-pane lock does NOT key off this: a live session with no edits yet
   *  (a plain "hi") must not lock the workbench for the whole chat. See
   *  push_state and `pulling` for what actually locks. */
  agent_active: boolean;
  /** A force-pull or three-way merge is writing to the clone's files right
   *  now (the watcher's pull leg, under its _op_lock) — the other condition,
   *  besides push_state pending/pushing, that locks the left pane: the
   *  clone's files are moving on disk. */
  pulling: boolean;
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
export const ackDelta = (name: string, rev: number, ok: boolean) =>
  postJson<{ ok: boolean; rev: number }>("/api/canvases/delta/ack", { name, rev, ok });

/** Pull the server's canvas into the clone now (the "Server changes · Pull"
 *  chip). Clean clone: force pull; dirty: three-way merge, canvas.toml per
 *  node, local edits kept. Returns the full status with `ok`. */
export const pullCanvas = (name: string) =>
  postJson<SyncStatus & { ok: boolean; error?: string | null }>(
    "/api/canvases/sync/pull",
    { name },
  );

export const startSync = (name: string) =>
  postJson<SyncStatus>("/api/canvases/sync/start", { name });

export const stopSync = (name: string) =>
  postJson<{ ok: boolean }>("/api/canvases/sync/stop", { name });

export const getSyncStatus = (name: string) =>
  getJson<SyncStatus>(`/api/canvases/sync/status?name=${encodeURIComponent(name)}`);

/** Spawn a Claude session on the canvas clone primed with the failing
 *  push's errors; attach the chat iframe with the returned run_id. */
export const fixWithClaude = (name: string) =>
  postJson<{ ok: boolean; run_id: string }>("/api/canvases/fix", { name });

export const getAccessToken = () =>
  getJson<{ access_token: string }>("/api/canvases/token", GUARD);
