// ONE LONG-POLL PER URL FOR THE WHOLE APP, across every document of the origin.
//
// WebKit caps connections per host:port at 6, and the cap is shared by every
// window of the app (one networking process). Each document used to hold its
// own 25 s `/api/tasks/changes` long-poll, and so did every same-origin iframe
// (the Bots page embeds `/tasks?embed=1`): four Bots windows filled all six
// sockets and everything else on that host — the health probe, the boot reads
// — queued behind them.
//
// This proxies a long-poll GET, keyed by its exact URL, through ONE leader
// document. Identical URLs share one upstream request and every document gets
// the answer over a BroadcastChannel. In steady state every loop is at the same
// generation, so the whole app holds ONE socket instead of 1–3 per document. A
// loop that is behind asks with an older `since`; the server answers that at
// once, so it is a short request and not a held socket.
//
// LEADERSHIP is a Web Lock (`navigator.locks`), held until the document goes
// away, so failover needs no code of its own: the browser frees the lock, the
// next document's callback runs `becomeLeader`, announces itself, and the
// followers re-send their pending requests to it. A follower whose leader
// vanished without a successor is unblocked by FOLLOWER_TIMEOUT_MS, and its
// caller's own backoff retries.
//
// Requests carry the id of the leader they are addressed to, and a leader acts
// only on requests addressed to it. That is what keeps a split-brain (two
// documents both believing they hold the lock) from fetching one request twice.
//
// Without BroadcastChannel or Web Locks this is a plain `fetch`.

/** How long a follower waits for a leader's answer before giving up with a
 *  TypeError. Longer than the 25 s server hold, so a healthy poll never trips it. */
export const FOLLOWER_TIMEOUT_MS = 40_000;

const CHANNEL_NAME = "fused-longpoll-v1";
const LOCK_NAME = "fused-longpoll-leader";

export interface ChannelLike {
  postMessage(m: unknown): void;
  onmessage: ((ev: { data: unknown }) => void) | null;
  close(): void;
}
export interface LocksLike {
  request(name: string, cb: () => Promise<unknown>): Promise<unknown>;
}
export interface SharedLongPollEnv {
  /** null ⇒ plain fetch, no sharing. */
  channel: ChannelLike | null;
  /** null ⇒ plain fetch, no sharing. */
  locks: LocksLike | null;
  fetch: (url: string, init: { signal: AbortSignal; cache: "no-store" }) => Promise<Response>;
  setTimeout: (fn: () => void, ms: number) => unknown;
  clearTimeout: (h: unknown) => void;
  /** crypto.randomUUID in the browser. */
  newId: () => string;
}

type Msg =
  | { v: 1; t: "who" }
  | { v: 1; t: "leader"; id: string }
  | { v: 1; t: "req"; id: string; to: string; url: string }
  | { v: 1; t: "cancel"; id: string; to: string }
  | { v: 1; t: "res"; url: string; status: number; body: string }
  | { v: 1; t: "err"; url: string };

interface Pending {
  url: string;
  resolve: (r: Response) => void;
  reject: (e: unknown) => void;
  timer: unknown;
  /** The leader this request was sent to; null while local or not yet sent. */
  sentTo: string | null;
  detach: () => void;
}

interface Inflight {
  ctl: AbortController;
  waiters: Set<string>;
}

const abortError = (): Error => {
  if (typeof DOMException === "function") return new DOMException("Aborted", "AbortError");
  const e = new Error("Aborted");
  e.name = "AbortError";
  return e;
};

const upstreamFailed = () => new TypeError("shared long-poll: upstream failed");

// 101/204/205/304 must not carry a body in a Response.
const NULL_BODY = new Set([101, 204, 205, 304]);
const makeResponse = (status: number, body: string): Response =>
  new Response(NULL_BODY.has(status) ? null : body, { status });

export function createSharedLongPoll(env: SharedLongPollEnv): {
  fetch(url: string, init?: { signal?: AbortSignal }): Promise<Response>;
  dispose(): void;
} {
  const { channel, locks } = env;
  const myId = env.newId();
  let isLeader = false;
  let leaderId: string | null = null;
  let started = false;
  let disposed = false;
  const pending = new Map<string, Pending>();
  const inflight = new Map<string, Inflight>();

  const post = (m: Msg) => {
    try {
      channel?.postMessage(m);
    } catch {
      /* a closed channel: the follower timeout covers it */
    }
  };

  // ── leader side ───────────────────────────────────────────────────────────

  /** Deliver one outcome to every LOCAL request for `url`. */
  function settleLocal(url: string, how: (p: Pending) => void) {
    for (const [id, p] of [...pending]) {
      if (p.url !== url) continue;
      env.clearTimeout(p.timer);
      p.detach();
      pending.delete(id);
      how(p);
    }
  }

  function serve(reqId: string, url: string) {
    let entry = inflight.get(url);
    if (entry) {
      entry.waiters.add(reqId);
      return;
    }
    const ctl = new AbortController();
    entry = { ctl, waiters: new Set([reqId]) };
    const mine = entry;
    inflight.set(url, mine);
    const done = () => {
      if (inflight.get(url) === mine) inflight.delete(url);
    };
    void (async () => {
      try {
        const res = await env.fetch(url, { signal: ctl.signal, cache: "no-store" });
        const body = await res.text();
        done();
        post({ v: 1, t: "res", url, status: res.status, body });
        settleLocal(url, (p) => p.resolve(makeResponse(res.status, body)));
      } catch {
        done();
        if (ctl.signal.aborted) return;
        post({ v: 1, t: "err", url });
        settleLocal(url, (p) => p.reject(upstreamFailed()));
      }
    })();
  }

  function unserve(reqId: string) {
    for (const [url, entry] of inflight) {
      if (!entry.waiters.delete(reqId)) continue;
      if (entry.waiters.size === 0) {
        inflight.delete(url);
        entry.ctl.abort();
      }
      return;
    }
  }

  function becomeLeader() {
    if (disposed) return;
    isLeader = true;
    leaderId = myId;
    post({ v: 1, t: "leader", id: myId });
    for (const [reqId, p] of pending) {
      p.sentTo = null;
      serve(reqId, p.url);
    }
  }

  // ── messages ──────────────────────────────────────────────────────────────

  function onMessage(ev: { data: unknown }) {
    const m = ev.data as Msg | null;
    if (!m || typeof m !== "object" || m.v !== 1) return;
    switch (m.t) {
      case "who":
        if (isLeader) post({ v: 1, t: "leader", id: myId });
        break;
      case "leader":
        // A leader that hears another leader ignores it: each serves its own.
        if (isLeader || m.id === myId) break;
        leaderId = m.id;
        for (const [reqId, p] of pending) {
          if (p.sentTo === m.id) continue;
          p.sentTo = m.id;
          post({ v: 1, t: "req", id: reqId, to: m.id, url: p.url });
        }
        break;
      case "req":
        if (isLeader && m.to === myId) serve(m.id, m.url);
        break;
      case "cancel":
        if (isLeader && m.to === myId) unserve(m.id);
        break;
      case "res":
        settleLocal(m.url, (p) => p.resolve(makeResponse(m.status, m.body)));
        break;
      case "err":
        settleLocal(m.url, (p) => p.reject(upstreamFailed()));
        break;
    }
  }

  function start() {
    if (started) return;
    started = true;
    channel!.onmessage = onMessage;
    post({ v: 1, t: "who" });
    try {
      // Held until the document goes away; the browser releases it then.
      locks!
        .request(LOCK_NAME, () => {
          becomeLeader();
          return new Promise<never>(() => {});
        })
        .catch(() => {
          /* no lock: stay a follower */
        });
    } catch {
      /* ditto */
    }
  }

  // ── the caller's side ─────────────────────────────────────────────────────

  function sharedFetch(url: string, init?: { signal?: AbortSignal }): Promise<Response> {
    if (!channel || !locks) {
      return env.fetch(url, { signal: init?.signal ?? new AbortController().signal, cache: "no-store" });
    }
    const signal = init?.signal;
    if (signal?.aborted || disposed) return Promise.reject(abortError());
    start();
    return new Promise<Response>((resolve, reject) => {
      const reqId = env.newId();
      const forget = () => {
        env.clearTimeout(p.timer);
        p.detach();
        pending.delete(reqId);
      };
      const cancelUpstream = () => {
        if (isLeader) unserve(reqId);
        else if (p.sentTo) post({ v: 1, t: "cancel", id: reqId, to: p.sentTo });
      };
      const onAbort = () => {
        if (!pending.has(reqId)) return;
        forget();
        cancelUpstream();
        reject(abortError());
      };
      const p: Pending = {
        url,
        resolve,
        reject,
        sentTo: null,
        timer: env.setTimeout(() => {
          if (!pending.has(reqId)) return;
          forget();
          cancelUpstream();
          reject(new TypeError("shared long-poll: no answer"));
        }, FOLLOWER_TIMEOUT_MS),
        detach: () => signal?.removeEventListener("abort", onAbort),
      };
      signal?.addEventListener("abort", onAbort, { once: true });
      pending.set(reqId, p);
      if (isLeader) serve(reqId, url);
      else if (leaderId) {
        p.sentTo = leaderId;
        post({ v: 1, t: "req", id: reqId, to: leaderId, url });
      }
      // else: sent when a leader is announced.
    });
  }

  function dispose() {
    disposed = true;
    for (const entry of inflight.values()) entry.ctl.abort();
    inflight.clear();
    for (const p of pending.values()) {
      env.clearTimeout(p.timer);
      p.detach();
      p.reject(abortError());
    }
    pending.clear();
    if (channel) {
      channel.onmessage = null;
      channel.close();
    }
  }

  return { fetch: sharedFetch, dispose };
}

let singleton: ReturnType<typeof createSharedLongPoll> | null = null;

/** The browser singleton, built lazily on first call. */
export function sharedLongPollFetch(url: string, init?: { signal?: AbortSignal }): Promise<Response> {
  if (!singleton) {
    let channel: ChannelLike | null = null;
    try {
      if (typeof BroadcastChannel === "function") channel = new BroadcastChannel(CHANNEL_NAME) as unknown as ChannelLike;
    } catch {
      channel = null;
    }
    const locks =
      typeof navigator !== "undefined" && navigator.locks
        ? (navigator.locks as unknown as LocksLike)
        : null;
    singleton = createSharedLongPoll({
      channel,
      locks,
      fetch: (u, i) => fetch(u, i),
      setTimeout: (fn, ms) => setTimeout(fn, ms),
      clearTimeout: (h) => clearTimeout(h as ReturnType<typeof setTimeout>),
      newId: () => globalThis.crypto?.randomUUID?.() ?? String(Math.random()).slice(2),
    });
  }
  return singleton.fetch(url, init);
}
