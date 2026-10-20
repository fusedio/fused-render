// Browser-side request wait time (the shell half; static/runtime.js inlines the
// same logic because it has no bundler).
//
// WHY: WebKit allows 6 HTTP/1.1 connections per host:port, shared by every
// window of the app. A request beyond that queues INSIDE the browser, so the
// server never sees the wait and "fused-render is slow" has no server-side
// evidence. Each document can see its own: for a resource timing entry,
// `requestStart - startTime` is the time before the request was sent.
//
// Measured only while someone is watching. The server adds
// `Server-Timing: fr;dur=…, fr-watch` to /api/ responses while a dashboard is
// open; only entries carrying `fr-watch` are collected, and a flush is sent at
// most every 10 s and only while such an entry is under 15 s old. Idle cost is
// one cheap observer callback per request and no timers.

export const BEACON_PATH = "/api/system/net/client";
export const WATCH_MS = 15_000;
export const FLUSH_MS = 10_000;
const MAX_SAMPLES = 64;
const MAX_ROUTES = 200;

const ID_HEX = /^[0-9a-fA-F]{8,}$/;
const ID_UUID = /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/;
const ID_NUM = /^\d+$/;

/** Same rule as fused_render/netlog.py normalize_route: query dropped, id-like
 *  segments (digits, 8+ hex, uuid, or longer than 40 chars) become `{id}`. */
export function normalizeRoute(path: string): string {
  const bare = path.split("?")[0].split("#")[0];
  return bare
    .split("/")
    .map((s) =>
      s && (s.length > 40 || ID_NUM.test(s) || ID_UUID.test(s) || ID_HEX.test(s)) ? "{id}" : s
    )
    .join("/");
}

/** The shell's page identity, by the server's netlog.source_name rule: the
 *  `path` query param (how the shell addresses a file) when present, else the
 *  pathname; percent-encoded the way runtime.js encodes X-Fused-Page, so the
 *  beacon's (source, route) keys join the server groups. */
export function shellPageIdentity(loc: { search: string; pathname: string }): string {
  const p = new URLSearchParams(loc.search).get("path");
  return encodeURIComponent(p || loc.pathname);
}

export interface TimingLike {
  name: string;
  startTime: number;
  requestStart?: number;
  connectStart?: number;
  fetchStart?: number;
  duration: number;
  serverTiming?: ReadonlyArray<{ name: string; duration: number }>;
}

/** Time the request waited before the browser sent it. */
export function queueMs(e: TimingLike): number {
  const sent = e.requestStart || e.connectStart || e.fetchStart || e.startTime;
  return Math.max(0, sent - e.startTime);
}

export function serverMs(e: TimingLike): number | null {
  const st = e.serverTiming?.find((s) => s.name === "fr");
  return st ? st.duration : null;
}

export function hasWatch(e: TimingLike): boolean {
  return !!e.serverTiming?.some((s) => s.name === "fr-watch");
}

export interface RouteStat {
  count: number;
  sum: number;
  max: number;
  samples: number[];
}

export interface CollectorOpts {
  /** The page identity, as X-Fused-Page carries it (shell: the route). */
  page: () => string;
  now: () => number;
  send: (json: string) => void;
  origin: string;
  setTimer: (fn: () => void, ms: number) => unknown;
}

/** Collects same-origin /api/ resource entries into per-route queue stats and
 *  flushes them as a beacon, under the watch rules above. */
export class NetCollector {
  private routes = new Map<string, RouteStat>();
  private lastWatch = -Infinity;
  private lastFlush = -Infinity;
  private timer: unknown = null;

  constructor(private o: CollectorOpts) {}

  add(e: TimingLike): void {
    let u: URL;
    try {
      u = new URL(e.name, this.o.origin);
    } catch {
      return;
    }
    if (u.origin !== this.o.origin || !u.pathname.startsWith("/api/")) return;
    if (u.pathname === BEACON_PATH || !hasWatch(e)) return;
    this.lastWatch = this.o.now();
    const route = normalizeRoute(u.pathname);
    let st = this.routes.get(route);
    if (!st) {
      if (this.routes.size >= MAX_ROUTES) return;
      st = { count: 0, sum: 0, max: 0, samples: [] };
      this.routes.set(route, st);
    }
    const q = Math.round(queueMs(e) * 10) / 10;
    st.count++;
    st.sum += q;
    if (q > st.max) st.max = q;
    if (st.samples.length >= MAX_SAMPLES) st.samples.shift();
    st.samples.push(q);
    this.schedule();
  }

  private schedule(): void {
    if (this.timer !== null) return;
    const wait = Math.max(0, FLUSH_MS - (this.o.now() - this.lastFlush));
    this.timer = this.o.setTimer(() => {
      this.timer = null;
      this.flush();
    }, wait);
  }

  /** Send what has accumulated, if someone is still watching. */
  flush(): boolean {
    const now = this.o.now();
    if (this.routes.size === 0 || now - this.lastWatch > WATCH_MS) {
      // Nobody is watching any more: drop, never send a stale batch later.
      if (now - this.lastWatch > WATCH_MS) this.routes.clear();
      return false;
    }
    if (now - this.lastFlush < FLUSH_MS) return false;
    const routes: Record<string, RouteStat> = {};
    for (const [k, v] of this.routes) routes[k] = v;
    this.routes.clear();
    this.lastFlush = now;
    this.o.send(JSON.stringify({ page: this.o.page(), routes }));
    return true;
  }
}

/** Start observing in this document. A no-op where PerformanceObserver or
 *  sendBeacon is missing. Returns the collector (tests) or null. */
export function installNetTiming(page: () => string): NetCollector | null {
  if (typeof PerformanceObserver === "undefined" || typeof navigator === "undefined") return null;
  if (typeof navigator.sendBeacon !== "function") return null;
  const collector = new NetCollector({
    page,
    now: () => performance.now(),
    send: (json) => {
      try {
        navigator.sendBeacon(BEACON_PATH, json);
      } catch {
        /* a beacon is best effort */
      }
    },
    origin: window.location.origin,
    setTimer: (fn, ms) => setTimeout(fn, ms),
  });
  try {
    const obs = new PerformanceObserver((list) => {
      for (const e of list.getEntries()) collector.add(e as PerformanceResourceTiming);
    });
    obs.observe({ type: "resource", buffered: true });
  } catch {
    return null;
  }
  return collector;
}
