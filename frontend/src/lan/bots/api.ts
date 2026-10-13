// Typed fetch wrappers for /api/phone/bots (the phone-bots wire contract).
// Every POST/DELETE carries `X-Fused: 1` (server.common._require_fused); the
// LAN listener stamps X-Fused-Device itself. A failed call throws an ApiError
// whose message is the server's own sentence ({"error": "…"}), so the page
// can show it as-is in a toast.
import type {
  BotPage,
  BotsList,
  CreateBody,
  Preset,
  RoutineBody,
  SendBody,
  SettingsBody,
} from "./types";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

const BASE = "/api/phone/bots";

async function call<T>(method: "GET" | "POST" | "DELETE", url: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = {};
  if (method !== "GET") headers["X-Fused"] = "1";
  if (body !== undefined) headers["Content-Type"] = "application/json";
  let res: Response;
  try {
    res = await fetch(url, {
      method,
      headers,
      cache: "no-store",
      credentials: "same-origin",
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new ApiError(0, "Can't reach the computer");
  }
  const data = (await res.json().catch(() => null)) as (T & { error?: unknown }) | null;
  if (!res.ok) {
    const msg = data && typeof data.error === "string" && data.error ? data.error : `HTTP ${res.status}`;
    throw new ApiError(res.status, msg);
  }
  return data as T;
}

const enc = encodeURIComponent;

export const api = {
  list: () => call<BotsList>("GET", BASE),
  presets: () => call<{ presets: Preset[] }>("GET", `${BASE}/presets`),
  page: (id: string, q: { since?: number; before?: number; limit?: number; detail?: boolean } = {}) => {
    const p = new URLSearchParams();
    p.set("since", String(q.since ?? 0));
    if (q.before != null) p.set("before", String(q.before));
    if (q.limit != null) p.set("limit", String(q.limit));
    if (q.detail) p.set("detail", "1");
    return call<BotPage>("GET", `${BASE}/${enc(id)}?${p}`);
  },
  send: (id: string, body: SendBody) =>
    call<{ ok: true; seq?: number; held?: boolean }>("POST", `${BASE}/${enc(id)}/send`, body),
  control: (id: string, op: "pause" | "resume" | "stop" | "clear") =>
    call<{ ok: true }>("POST", `${BASE}/${enc(id)}/${op}`, {}),
  create: (body: CreateBody) => call<{ ok: true; id: string }>("POST", BASE, body),
  settings: (id: string, body: SettingsBody) => call<{ ok: true }>("POST", `${BASE}/${enc(id)}/settings`, body),
  routines: (id: string, body: RoutineBody) => call<{ ok: true }>("POST", `${BASE}/${enc(id)}/routines`, body),
  remove: (id: string) => call<{ ok: true }>("DELETE", `${BASE}/${enc(id)}`),
};
