// Typed bridge to fused_render/canvases.py — same transport as the shared
// helpers (thrown HttpError, X-Fused write guard). The guarded GETs (list,
// whoami, token) execute a CLI child / hand out a credential, so they carry
// the X-Fused header too (the server 403s them without it).
import { getJson, postJson } from "@platform/lib/api";

const GUARD = { headers: { "X-Fused": "1" } };

export interface CanvasesStatus {
  cli_found: boolean;
  logged_in: boolean;
  login_in_flight: boolean;
  workbench_base_url: string;
  canvases_dir: string;
}

export interface CanvasEntry {
  name: string;
  id: string | null;
  cloned: boolean;
}

export interface SyncStatus {
  name: string;
  dir: string;
  watching: boolean;
  push_state: "idle" | "pending" | "pushing" | "error";
  push_seq: number;
  last_push_at: number | null;
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

export const cloneCanvas = (name: string) =>
  postJson<{ ok: boolean; dir: string }>("/api/canvases/clone", { name });

export const startSync = (name: string) =>
  postJson<SyncStatus>("/api/canvases/sync/start", { name });

export const stopSync = (name: string) =>
  postJson<{ ok: boolean }>("/api/canvases/sync/stop", { name });

export const getSyncStatus = (name: string) =>
  getJson<SyncStatus>(`/api/canvases/sync/status?name=${encodeURIComponent(name)}`);

export const getAccessToken = () =>
  getJson<{ access_token: string }>("/api/canvases/token", GUARD);
