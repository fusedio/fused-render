// Typed fetch wrappers for /api/phone/tasks (docs/phone-tasks.md §2). Every
// POST carries `X-Fused: 1` (server.common._require_fused); the LAN listener
// stamps X-Fused-Device itself. A failed call throws the bots door's ApiError,
// whose message is the server's own sentence ({"error": "…"}).
import { ApiError } from "../bots/api";
import type { CreateResult, NewAppResult, TaskDetail, TasksListData, Unchanged } from "./types";

export { ApiError };

const BASE = "/api/phone/tasks";

async function call<T>(method: "GET" | "POST", url: string, body?: unknown): Promise<T> {
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
  list: (q: { gen?: string; archived?: boolean; limit?: number } = {}) => {
    const p = new URLSearchParams();
    if (q.gen) p.set("gen", q.gen);
    if (q.archived) p.set("archived", "1");
    if (q.limit != null) p.set("limit", String(q.limit));
    const qs = p.toString();
    return call<TasksListData | Unchanged>("GET", qs ? `${BASE}?${qs}` : BASE);
  },
  detail: (key: string, q: { since?: number; last?: string; before?: number; limit?: number } = {}) => {
    const p = new URLSearchParams();
    if (q.since != null) p.set("since", String(q.since));
    if (q.last) p.set("last", q.last);
    if (q.before != null) p.set("before", String(q.before));
    if (q.limit != null) p.set("limit", String(q.limit));
    const qs = p.toString();
    return call<TaskDetail>("GET", qs ? `${BASE}/${enc(key)}?${qs}` : `${BASE}/${enc(key)}`);
  },
  create: (body: { prompt: string; target: string }) => call<CreateResult>("POST", BASE, body),
  newApp: (body: { prompt: string; name?: string }) => call<NewAppResult>("POST", `${BASE}/new-app`, body),
  send: (key: string, text: string) => call<{ queued: boolean; key: string }>("POST", `${BASE}/${enc(key)}/send`, { text }),
  cancel: (key: string) => call<{ cancelled: true }>("POST", `${BASE}/${enc(key)}/cancel`, {}),
  decide: (key: string, request_id: string, decision: "allow" | "deny") =>
    call<{ held: boolean }>("POST", `${BASE}/${enc(key)}/decide`, { request_id, decision }),
  answer: (key: string, request_id: string, answers: Record<string, string[]>, custom?: Record<string, string>) =>
    call<{ held: boolean }>("POST", `${BASE}/${enc(key)}/answer`, { request_id, answers, ...(custom ? { custom } : {}) }),
  op: (key: string, op: "archive" | "unarchive" | "delete" | "read" | "skip") =>
    call<Record<string, unknown>>("POST", `${BASE}/${enc(key)}/${op}`, {}),
  rename: (key: string, title: string) => call<Record<string, unknown>>("POST", `${BASE}/${enc(key)}/rename`, { title }),
  favourite: (key: string, on: boolean) => call<Record<string, unknown>>("POST", `${BASE}/${enc(key)}/favourite`, { on }),
};
