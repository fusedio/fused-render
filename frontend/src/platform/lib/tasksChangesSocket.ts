// `/api/tasks/changes` over ONE WebSocket per document (routers/tasks.py
// `api_tasks_changes_ws`), with the GET kept as the fallback.
//
// WHY (measured 2026-10-08). The native app's WKWebView windows all share one
// data store, and WebKit allows SIX HTTP/1.1 connections per host:port across
// every one of them. Each shell document — the top window, each embed pane, a
// page's `fused.tasks.watch` — parked a 25 s GET long-poll on the changes
// endpoint, which on their own filled the six; every other fetch (the boot
// reads, /api/run, icons) then queued browser-side behind them. WebSockets are
// not counted against that cap (measured the same day — the same reason
// `/api/fs/events` is a socket, D74), so the long-polls move here: one socket
// for the document, requests multiplexed over it by id, however many loops
// (the shell's listing feed, a bots build) are watching.
//
// THE CALLERS KEEP THEIR OWN LOOPS. This is a transport, not a feed: the
// generation cursor, the 3 s error backoff, the visibility park and the floor
// re-read all stay where they were. `requestTasksChanges` answers exactly what
// the GET would have, or throws where the GET's `!res.ok` would have — so a
// socket that drops mid-wait rejects its in-flight requests, the caller's
// backoff runs, and its next request reconnects.
//
// THE FALLBACK IS FOR THE DOCUMENT'S LIFETIME, and is decided once: if the
// socket fails to open, or closes before ANY socket of this document has
// delivered a reply, nothing here is going to work (a LAN peer, whose wrapper
// 1008-closes this route; a proxy without upgrade; a sandboxed page whose
// `Origin: null` the server refuses), so the transport becomes HTTP and the
// request that found out is re-asked over the caller's GET — it never sees the
// failure. A socket that HAS answered and then drops is a server restart, not
// a missing feature, and reconnects with backoff instead.
//
// runtime.js carries its own plain-JS copy of this (a page has no bundler);
// keep the two in step.

/** The GET's query params, plus `page` — what `X-Fused-Page` would carry
 *  (percent-encoded), since a WebSocket cannot set headers. */
export interface TasksChangesParams {
  since: number;
  wait: number;
  under?: string;
  scope?: string;
  page?: string;
}

/** A refusal the GET would have answered with a status (400 bad scope, 500). */
export class TasksChangesError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

type Transport = "unknown" | "ws" | "http";

interface Pending {
  sock: WebSocket;
  resolve(v: Record<string, unknown>): void;
  reject(e: unknown): void;
  /** Re-ask over the caller's GET (the socket turned out not to work at all). */
  fallback(): void;
  timer: ReturnType<typeof setTimeout>;
  unabort(): void;
}

let transport: Transport = "unknown";
let sock: WebSocket | null = null;
let opening: Promise<WebSocket> | null = null;
let nextId = 0;
const pending = new Map<number, Pending>();
/** Reconnect backoff after a socket that had worked went away: nothing dials
 *  again before `retryAt`, and the gap doubles up to RETRY_MAX_MS until a reply
 *  lands. The callers' own 3 s backoff paces the retries in between. */
let retryAt = 0;
let retryMs = 0;
const RETRY_MIN_MS = 1000;
const RETRY_MAX_MS = 30_000;
/** How long past its own `wait` a request may go unanswered before it is
 *  given up on — a half-open socket (a machine waking from sleep) would
 *  otherwise park the caller's loop for ever, where a GET would have errored. */
const REPLY_GRACE_MS = 15_000;

function socketUsable(): boolean {
  // A real browser document only. The bun test shim has a `location` with no
  // `host` (and bun has a global WebSocket), so suites keep driving the GET
  // they mock rather than dialling a server that is not there.
  return (
    typeof WebSocket === "function" &&
    typeof location !== "undefined" &&
    !!location.host &&
    (location.protocol === "http:" || location.protocol === "https:")
  );
}

function socketUrl(): string {
  const proto = location.protocol === "https:" ? "wss://" : "ws://";
  return proto + location.host + "/api/tasks/changes/ws";
}

/** The socket failed before it ever answered anything: HTTP from here on, and
 *  every request still waiting on it is re-asked over its GET. */
function giveUp(on: WebSocket) {
  transport = "http";
  for (const [id, p] of Array.from(pending)) {
    if (p.sock !== on) continue;
    pending.delete(id);
    clearTimeout(p.timer);
    p.unabort();
    p.fallback();
  }
}

function dropAll(on: WebSocket) {
  retryMs = Math.min(RETRY_MAX_MS, retryMs ? retryMs * 2 : RETRY_MIN_MS);
  retryAt = Date.now() + retryMs;
  for (const [id, p] of Array.from(pending)) {
    if (p.sock !== on) continue;
    pending.delete(id);
    clearTimeout(p.timer);
    p.unabort();
    p.reject(new Error("tasks changes socket closed"));
  }
}

function connect(): Promise<WebSocket> {
  if (sock && sock.readyState === WebSocket.OPEN) return Promise.resolve(sock);
  if (opening) return opening;
  const attempt = new Promise<WebSocket>((resolve, reject) => {
    let s: WebSocket;
    try {
      s = new WebSocket(socketUrl());
    } catch (e) {
      reject(e);
      return;
    }
    let opened = false;
    s.onopen = () => {
      opened = true;
      sock = s;
      resolve(s);
    };
    s.onmessage = (ev) => {
      let msg: Record<string, unknown>;
      try {
        msg = JSON.parse(String(ev.data));
      } catch {
        return;
      }
      if (!msg || typeof msg !== "object") return;
      // The first reply is the proof this transport works for the document.
      transport = "ws";
      retryMs = 0;
      retryAt = 0;
      const id = msg.id;
      if (typeof id !== "number") return;
      const p = pending.get(id);
      if (!p) return; // aborted, or timed out, while the server was answering
      pending.delete(id);
      clearTimeout(p.timer);
      p.unabort();
      if (typeof msg.error === "string") {
        p.reject(new TasksChangesError(msg.error, typeof msg.status === "number" ? msg.status : 500));
        return;
      }
      delete msg.id;
      p.resolve(msg);
    };
    // `error` is always followed by `close`, which does the bookkeeping.
    s.onerror = () => {};
    s.onclose = () => {
      if (sock === s) sock = null;
      if (!opened) {
        reject(new Error("tasks changes socket did not open"));
        return;
      }
      if (transport === "unknown") giveUp(s);
      else dropAll(s);
    };
  });
  opening = attempt;
  const clear = () => {
    if (opening === attempt) opening = null;
  };
  attempt.then(clear, clear);
  return attempt;
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

/**
 * One `/api/tasks/changes` question. Over the document's socket while that
 * works; over `http()` — the caller's own GET, unchanged — when it does not.
 * Resolves with the GET's JSON answer; rejects where the GET would have
 * (a refusal, a dropped connection, an abort), so the caller's existing
 * backoff and abort handling cover both transports.
 */
export async function requestTasksChanges<T = Record<string, unknown>>(
  params: TasksChangesParams,
  http: () => Promise<T>,
  signal?: AbortSignal,
): Promise<T> {
  if (transport === "http" || !socketUsable()) return http();
  if (signal?.aborted) throw abortError();
  if (transport === "ws" && !(sock && sock.readyState === WebSocket.OPEN) && Date.now() < retryAt) {
    throw new Error("tasks changes socket reconnecting");
  }
  let s: WebSocket;
  try {
    s = await connect();
  } catch (e) {
    // Never opened. Before any reply ever landed that is "this document has
    // no socket" — HTTP for good, and this request goes there now. After, it
    // is a server that is down or restarting: reject, back off, retry.
    if (transport === "unknown") {
      transport = "http";
      return http();
    }
    retryMs = Math.min(RETRY_MAX_MS, retryMs ? retryMs * 2 : RETRY_MIN_MS);
    retryAt = Date.now() + retryMs;
    throw e;
  }
  if (signal?.aborted) throw abortError();
  const id = ++nextId;
  return new Promise<T>((resolve, reject) => {
    const onAbort = () => {
      if (!pending.delete(id)) return;
      clearTimeout(entry.timer);
      // Tell the server too, so its waiter goes now rather than in 25 s.
      try {
        if (s.readyState === WebSocket.OPEN) s.send(JSON.stringify({ cancel: id }));
      } catch {
        /* the socket is going anyway */
      }
      reject(abortError());
    };
    const entry: Pending = {
      sock: s,
      resolve: (v) => resolve(v as T),
      reject,
      fallback: () => {
        http().then(resolve, reject);
      },
      timer: setTimeout(() => {
        if (!pending.delete(id)) return;
        entry.unabort();
        try {
          s.send(JSON.stringify({ cancel: id }));
        } catch {
          /* already closed */
        }
        // No reply past its own wait + grace: the socket is half-open.
        // Leaving it current would time out every later request too, so
        // close it — onclose then settles the rest and backs off, and the
        // next request redials.
        if (sock === s) {
          try {
            s.close();
          } catch {
            /* already closing */
          }
        }
        reject(new Error("tasks changes socket: no reply"));
      }, Math.max(0, params.wait) * 1000 + REPLY_GRACE_MS),
      unabort: () => signal?.removeEventListener("abort", onAbort),
    };
    pending.set(id, entry);
    signal?.addEventListener("abort", onAbort);
    const msg: Record<string, unknown> = { id, since: params.since, wait: params.wait };
    if (params.under) msg.under = params.under;
    if (params.scope) msg.scope = params.scope;
    if (params.page) msg.page = params.page;
    try {
      s.send(JSON.stringify(msg));
    } catch (e) {
      pending.delete(id);
      clearTimeout(entry.timer);
      entry.unabort();
      reject(e);
    }
  });
}
