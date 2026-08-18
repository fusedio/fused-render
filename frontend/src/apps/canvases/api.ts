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
  /** Canvas preview image (presigned or public URL) from the server; null when none. */
  preview_url: string | null;
  /** Control-plane last_updated (epoch seconds); null on the external-CLI fallback. */
  updated_at: number | null;
}

export interface SyncStatus {
  name: string;
  dir: string;
  watching: boolean;
  push_state: "idle" | "pending" | "pushing" | "error";
  push_seq: number;
  last_push_at: number | null;
  /** Increments when the watcher pulls remote (workbench-side) changes down. */
  pull_seq: number;
  last_pull_at: number | null;
  error: string | null;
  /** Full per-line CLI output of the failing push (e.g. one validation
   *  error per line); empty when the last push succeeded. */
  error_detail: string[];
}

export const getCanvasesStatus = () => getJson<CanvasesStatus>("/api/canvases/status");

export const getWhoami = () =>
  getJson<{ handle: string | null }>("/api/canvases/whoami", GUARD);

export const startLogin = () => postJson<{ ok: boolean }>("/api/canvases/login", {});

export const cancelLogin = () =>
  postJson<{ ok: boolean }>("/api/canvases/login/cancel", {});

export const listCanvases = () =>
  getJson<{ canvases: CanvasEntry[] }>("/api/canvases/list", GUARD);

export const createCanvas = (name: string) =>
  postJson<{ ok: boolean; name: string }>("/api/canvases/create", { name });

export const logout = () => postJson<{ ok: boolean }>("/api/canvases/logout", {});

export const cloneCanvas = (name: string) =>
  postJson<{ ok: boolean; dir: string }>("/api/canvases/clone", { name });

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
