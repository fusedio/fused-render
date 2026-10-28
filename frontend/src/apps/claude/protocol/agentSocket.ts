// The chat's `/api/claude/agent` and `/api/claude/artifacts` calls over ONE
// WebSocket per document (routers/claude_agent.py `api_claude_agent_ws`), with
// the POST kept as the fallback.
//
// WHY. Every chat poll (~400 ms) and send is a POST that holds one of WebKit's
// six per-host HTTP/1.1 connections, a pool shared by every native window of
// the app; WebSockets are not counted against it (the reason
// `tasksChangesSocket.ts` and `/api/run/ws` exist). This is a transport only:
// the caller (`agent.ts` `post`) keeps its own poll loop, error mapping and
// cadence, and a reply carries the exact JSON body the POST would have.
//
// `agentSocketCall` returns `null` when the socket is not usable right now
// (bun tests, no WebSocket, the fallback latched, a reconnect backoff in
// progress): the caller then fetches. A returned promise resolves `null` when
// the socket never OPENED, which also means "fetch" — nothing was sent, so
// re-asking over POST cannot double-run anything.
//
// THE FALLBACK IS FOR THE DOCUMENT'S LIFETIME, decided once: a socket that
// never opens, or closes before ANY reply ever arrived, latches POST (a LAN
// peer, whose wrapper 1008s this route; a proxy without upgrade; an `Origin:
// null` page). A socket that HAS answered and then drops is a server restart:
// reconnect with backoff, and requests made meanwhile go to POST.
//
// A request in flight on a socket that drops is REJECTED, never re-run: a
// poll re-issue is harmless, a `send` replay is not. The rejection text
// matches `trouble.ts`'s NETWORK regex so `isNetworkFailure` and the poll's
// transient-retry budget treat it like a failed fetch.

import { sourceHeader } from "@platform/lib/api";

export type AgentSocketRoute = "agent" | "artifacts";

export interface AgentSocketReply {
  status: number;
  body: unknown;
}

interface Pending {
  sock: WebSocket;
  resolve(v: AgentSocketReply): void;
  reject(e: unknown): void;
  timer: ReturnType<typeof setTimeout>;
  unabort(): void;
}

let latched = false;
let proven = false;
let sock: WebSocket | null = null;
let opening: Promise<WebSocket | null> | null = null;
let nextId = 0;
const pending = new Map<number, Pending>();
let retryAt = 0;
let retryMs = 0;
const RETRY_MIN_MS = 1000;
const RETRY_MAX_MS = 30_000;
/** The server's longest budget is 60 s (`pool.DEFAULT_BUDGET_S`) plus grace. */
const REPLY_TIMEOUT_MS = 75_000;
/** HOW LONG A HANDSHAKE MAY HANG. A proxy that accepts the upgrade and never
 *  answers it (a dev server without `ws: true`, a middlebox) leaves the socket
 *  CONNECTING for ever — no `open`, no `close` — and `connect()` used to wait
 *  for ever with it, so the chat's first call (a `shots_dir`, a poll) never
 *  returned and never fell back. Past this the attempt is closed by hand,
 *  which runs the never-opened bookkeeping below: latch POST, resolve null,
 *  the caller fetches. */
let connectTimeoutMs = 8000;
export function setAgentSocketConnectTimeoutForTests(ms: number): void {
  connectTimeoutMs = ms;
}

/** Test hook: forget every latch and pending request. */
export function resetAgentSocketForTests(): void {
  for (const p of pending.values()) clearTimeout(p.timer);
  pending.clear();
  try {
    sock?.close();
  } catch {
    /* already closed */
  }
  latched = false;
  proven = false;
  sock = null;
  opening = null;
  nextId = 0;
  retryAt = 0;
  retryMs = 0;
}

function socketUsable(): boolean {
  // A real browser document only: the bun test shim has a `location` with no
  // `host`, so suites keep driving the fetch they mock.
  return (
    typeof WebSocket === "function" &&
    typeof location !== "undefined" &&
    !!location.host &&
    (location.protocol === "http:" || location.protocol === "https:")
  );
}

function socketUrl(): string {
  return (location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/api/claude/agent/ws";
}

function closedError(): Error {
  return new TypeError("Failed to fetch: agent socket closed");
}

function abortError(): Error {
  try {
    return new DOMException("The operation was aborted.", "AbortError");
  } catch {
    const e = new Error("The operation was aborted.");
    e.name = "AbortError";
    return e;
  }
}

function backoff() {
  retryMs = Math.min(RETRY_MAX_MS, retryMs ? retryMs * 2 : RETRY_MIN_MS);
  retryAt = Date.now() + retryMs;
}

function rejectAll(on: WebSocket) {
  for (const [id, p] of Array.from(pending)) {
    if (p.sock !== on) continue;
    pending.delete(id);
    clearTimeout(p.timer);
    p.unabort();
    p.reject(closedError());
  }
}

function connect(): Promise<WebSocket | null> {
  if (sock && sock.readyState === WebSocket.OPEN) return Promise.resolve(sock);
  if (opening) return opening;
  const attempt = new Promise<WebSocket | null>((resolve) => {
    let s: WebSocket;
    try {
      s = new WebSocket(socketUrl());
    } catch {
      if (!proven) latched = true;
      else backoff();
      resolve(null);
      return;
    }
    let opened = false;
    const handshake = setTimeout(() => {
      if (opened) return;
      try {
        s.close();
      } catch {
        /* already closing */
      }
      // A socket closed from CONNECTING fires `close` in a browser; a shim
      // may not, so the never-opened road is walked here as well. Idempotent
      // with `onclose` below: `resolve` is single-shot.
      if (!proven) latched = true;
      else backoff();
      resolve(null);
    }, connectTimeoutMs);
    s.onopen = () => {
      opened = true;
      clearTimeout(handshake);
      sock = s;
      resolve(s);
    };
    s.onmessage = (ev) => {
      let msg: { id?: unknown; status?: unknown; body?: unknown } | null;
      try {
        msg = JSON.parse(String(ev.data));
      } catch {
        return;
      }
      if (!msg || typeof msg !== "object") return;
      proven = true;
      retryMs = 0;
      retryAt = 0;
      if (typeof msg.id !== "number") return;
      const p = pending.get(msg.id);
      if (!p) return; // aborted or timed out while the server was answering
      pending.delete(msg.id);
      clearTimeout(p.timer);
      p.unabort();
      p.resolve({ status: typeof msg.status === "number" ? msg.status : 500, body: msg.body ?? null });
    };
    // `error` is always followed by `close`, which does the bookkeeping.
    s.onerror = () => {};
    s.onclose = () => {
      clearTimeout(handshake);
      if (sock === s) sock = null;
      if (!opened) {
        if (!proven) latched = true;
        else backoff();
        resolve(null);
        return;
      }
      if (!proven) latched = true;
      else backoff();
      rejectAll(s);
    };
  });
  opening = attempt;
  const clear = () => {
    if (opening === attempt) opening = null;
  };
  attempt.then(clear, clear);
  return attempt;
}

/**
 * One chat call over the socket, or `null` when the caller should fetch.
 * `headers` are what the POST would send (X-Fused-Page/-Target/-Call); the
 * ambient `X-Fused-Source` is added here. Resolves with the reply's status and
 * the POST's JSON body; resolves `null` if the socket never opened; rejects
 * with a network-shaped error when the socket drops mid-flight, and with an
 * AbortError on abort (the server still finishes the work, as for an aborted
 * POST).
 */
export function agentSocketCall(
  route: AgentSocketRoute,
  headers: Record<string, string>,
  body: Record<string, unknown>,
  signal?: AbortSignal,
): Promise<AgentSocketReply | null> | null {
  if (latched || !socketUsable()) return null;
  if (!(sock && sock.readyState === WebSocket.OPEN) && Date.now() < retryAt) return null;
  return (async () => {
    if (signal?.aborted) throw abortError();
    const s = await connect();
    if (!s) return null;
    if (signal?.aborted) throw abortError();
    const id = ++nextId;
    return new Promise<AgentSocketReply>((resolve, reject) => {
      const onAbort = () => {
        if (!pending.delete(id)) return;
        clearTimeout(entry.timer);
        reject(abortError());
      };
      const entry: Pending = {
        sock: s,
        resolve,
        reject,
        timer: setTimeout(() => {
          if (!pending.delete(id)) return;
          entry.unabort();
          // No reply in the longest budget plus grace: the socket is
          // half-open. Close it so the next call redials.
          if (sock === s) {
            try {
              s.close();
            } catch {
              /* already closing */
            }
          }
          reject(closedError());
        }, REPLY_TIMEOUT_MS),
        unabort: () => signal?.removeEventListener("abort", onAbort),
      };
      pending.set(id, entry);
      signal?.addEventListener("abort", onAbort);
      try {
        s.send(JSON.stringify({ id, route, headers: { ...sourceHeader(), ...headers }, body }));
      } catch {
        pending.delete(id);
        clearTimeout(entry.timer);
        entry.unabort();
        reject(closedError());
      }
    });
  })();
}
