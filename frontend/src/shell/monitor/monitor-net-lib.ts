// The Monitor dashboard tabs' pure rules: URL params, the connection-slots
// strip, the live request list's merge, and the Overview tiles with their
// colours. No React and no network (monitor-api.ts owns the wire), so the
// suite can pin each rule directly. Tile thresholds mirror
// fused_render/sysmon/verdict.py: a tile is red or amber exactly when the
// verdict would name it, so the banner and the grid never disagree.
import { formatBytes } from "@platform/lib/sysmon";
import {
  sectionOk,
  type Dashboard,
  type Level,
  type NetRow,
  type NetSummary,
  type TimelinePoint,
} from "./monitor-api";
import type { SparkPoint } from "./Sparkline";

export type Tab = "overview" | "network" | "processes";
export const TABS: Tab[] = ["overview", "network", "processes"];

export interface MonitorParams {
  tab: Tab;
  source: string;
  route: string;
  pid: number | null;
}

/** `?tab=&source=&route=&pid=`, read once at mount. Anything unrecognised is
 *  the default, never an error: a stale link still opens the page. */
export function parseMonitorParams(search: string): MonitorParams {
  const q = new URLSearchParams(search);
  const tab = q.get("tab") as Tab | null;
  const pidRaw = q.get("pid");
  const pid = pidRaw !== null && /^\d+$/.test(pidRaw) ? Number(pidRaw) : null;
  return {
    tab: tab && TABS.includes(tab) ? tab : "overview",
    source: q.get("source") ?? "",
    route: q.get("route") ?? "",
    pid,
  };
}

export function monitorUrl(p: {
  tab: Tab;
  source?: string;
  route?: string;
  pid?: number | null;
}): string {
  const q = new URLSearchParams();
  if (p.tab !== "overview") q.set("tab", p.tab);
  if (p.source) q.set("source", p.source);
  if (p.route) q.set("route", p.route);
  if (p.pid != null) q.set("pid", String(p.pid));
  const s = q.toString();
  return s ? `/monitor?${s}` : "/monitor";
}

/** The browser's per-host HTTP/1.1 connection limit. */
export const SLOTS = 6;

/** `open` requests laid into `slots` boxes, oldest first (the one that has
 *  held its connection longest is box 1); the rest are what the browser is
 *  queueing. */
export function slotStrip(open: NetRow[], slots = SLOTS): { boxes: (NetRow | null)[]; queued: number } {
  const sorted = [...open].sort((a, b) => a.seq - b.seq);
  const boxes: (NetRow | null)[] = [];
  for (let i = 0; i < slots; i++) boxes.push(sorted[i] ?? null);
  return { boxes, queued: Math.max(0, sorted.length - slots) };
}

/** The client the strip draws: whichever has the most requests in flight. */
export function mainClient(summary: Pick<NetSummary, "in_flight"> | null | undefined): string | null {
  const c = summary?.in_flight?.[0];
  if (!c) return null;
  return summary!.in_flight.reduce((a, b) => (b.in_flight > a.in_flight ? b : a)).client;
}

export const ROW_LIMIT = 2000;

/** Merge a poll into the list: by id (a request seen open is replaced by its
 *  finished row), ordered by seq, the oldest dropped past `cap`. */
export function appendRows(cur: NetRow[], incoming: NetRow[], cap = ROW_LIMIT): NetRow[] {
  const byId = new Map<string | number, NetRow>();
  for (const r of cur) byId.set(r.id, r);
  for (const r of incoming) byId.set(r.id, r);
  const all = [...byId.values()].sort((a, b) => a.seq - b.seq);
  return all.length > cap ? all.slice(all.length - cap) : all;
}

export type TileLevel = Level | "unavailable";
export interface Tile {
  key: string;
  title: string;
  level: TileLevel;
  value: string;
  baseline?: string;
  tab: Tab;
}

const UNAVAILABLE = "unavailable";
const GB = 1 << 30;
// verdict.py's constants.
const SATURATED_SLOTS = 6;
const SATURATED_S = 2;
const QUEUE_P95_MS = 500;
const POOL_BUSY_FRAC = 0.9;
const LOOP_LAG_MS = 250;
const SWAP_GROWTH_BYTES = 500 * (1 << 20);
const CLAUDE_MAX_COUNT = 8;
const CLAUDE_MAX_BYTES = 4 * GB;
const MIN_FREE_BYTES = 5 * GB;
const RECENT_OUTAGE_S = 600;

const ms = (n: number | null | undefined) => (n == null ? "-" : `${Math.round(n)} ms`);

export function overviewTiles(d: Dashboard): Tile[] {
  const tiles: Tile[] = [];
  const add = (
    key: string,
    title: string,
    tab: Tab,
    section: unknown,
    fn: () => { level: Level; value: string; baseline?: string },
  ) => {
    if (!sectionOk(section as object)) {
      tiles.push({ key, title, tab, level: "unavailable", value: UNAVAILABLE });
      return;
    }
    try {
      tiles.push({ key, title, tab, ...fn() });
    } catch {
      tiles.push({ key, title, tab, level: "unavailable", value: UNAVAILABLE });
    }
  };

  const net = sectionOk(d.net) ? d.net : null;
  add("requests", "Requests", "network", d.net, () => {
    const s = net!.summary;
    const sat = s.in_flight.some(
      (c) => c.in_flight >= SATURATED_SLOTS && c.saturated_s > SATURATED_S,
    );
    const worst = Math.max(0, ...s.in_flight.map((c) => c.in_flight));
    return {
      level: sat ? "bad" : "ok",
      value: `${s.rps_10s}/s · ${worst} in flight`,
    };
  });
  add("queue", "Browser queue", "network", d.net, () => {
    const q = net!.summary.browser_queue_p95_ms;
    return { level: q != null && q > QUEUE_P95_MS ? "warn" : "ok", value: q == null ? "no data" : `${ms(q)} p95` };
  });
  add("server", "Server", "overview", d.server, () => {
    const s = d.server as Extract<Dashboard["server"], { loop_lag: unknown }>;
    const pool = s.pool;
    const lag = s.loop_lag?.current_ms;
    const busy = !!pool && pool.total > 0 && pool.borrowed / pool.total >= POOL_BUSY_FRAC;
    const lagging = lag != null && lag > LOOP_LAG_MS;
    return {
      level: busy || lagging ? "bad" : "ok",
      value: `${pool ? `${pool.borrowed}/${pool.total} threads` : "threads ?"} · lag ${ms(lag)}`,
    };
  });
  add("memory", "Memory and swap", "processes", d.memory, () => {
    const m = d.memory as Extract<Dashboard["memory"], { used: unknown }>;
    const growth = m.swap_growth_10m_bytes ?? 0;
    const level: Level =
      m.pressure === "critical" ? "bad" : m.pressure === "warn" || growth > SWAP_GROWTH_BYTES ? "warn" : "ok";
    const base = sectionOk(d.baseline) ? d.baseline.swap_used : null;
    return {
      level,
      value: `${formatBytes(m.used, true)} / ${formatBytes(m.total, true)} · swap ${formatBytes(m.swap_used, true)}`,
      baseline: base != null ? `usual swap ${formatBytes(base, true)}` : undefined,
    };
  });
  add("cpu", "CPU and thermal", "processes", d.cpu, () => {
    const c = d.cpu as Extract<Dashboard["cpu"], { cpu_pct: unknown }>;
    const throttled = c.thermal_limit != null && c.thermal_limit < 100;
    const base = sectionOk(d.baseline) ? d.baseline.load : null;
    const load = c.load?.[0];
    return {
      level: throttled || c.low_power ? "warn" : "ok",
      value: `${c.cpu_pct == null ? "-" : Math.round(c.cpu_pct)}%${load != null ? ` · load ${load.toFixed(1)}` : ""}${throttled ? ` · throttled ${c.thermal_limit}%` : ""}`,
      baseline: base != null ? `usual load ${base.toFixed(1)}` : undefined,
    };
  });
  add("claude", "Claude CLIs", "processes", d.claude, () => {
    const c = d.claude as Extract<Dashboard["claude"], { total: number }>;
    return {
      level: c.total > CLAUDE_MAX_COUNT || c.mem_bytes > CLAUDE_MAX_BYTES ? "warn" : "ok",
      value: `${c.total} · ${formatBytes(c.mem_bytes, true)}`,
    };
  });
  add("calls", "Python calls", "network", d.calls, () => {
    const c = d.calls as Extract<Dashboard["calls"], { top_overhead: unknown }>;
    const top = c.top_overhead[0];
    return {
      level: "ok",
      value: top ? `${top.entrypoint}: ${ms(top.overhead_p95_ms)} overhead` : "no calls",
    };
  });
  add("index", "Index", "overview", d.index, () => {
    const i = d.index as Extract<Dashboard["index"], object>;
    const bad = (i.waiting ?? 0) > 0 || (i.abandoned ?? 0) > 0;
    return {
      level: bad ? "warn" : "ok",
      value: `${i.waiting ?? 0} waiting · ${i.abandoned ?? 0} abandoned · ${i.scans?.running ?? 0} scans`,
    };
  });
  add("ai", "AI workers", "processes", d.ai, () => {
    const a = d.ai as Extract<Dashboard["ai"], { workers: unknown }>;
    const busy = a.workers.filter((w) => w.in_flight > 0).length;
    return { level: "ok", value: `${a.workers.length} resident · ${busy} busy` };
  });
  add("jobs", "Jobs and installs", "overview", d.jobs, () => {
    const j = d.jobs as Extract<Dashboard["jobs"], { running: number }>;
    const fails = sectionOk(d.installs) ? d.installs.failures.length : 0;
    return {
      level: j.stalled > 0 || fails > 0 ? "warn" : "ok",
      value: `${j.running} running · ${j.stalled} stalled${fails ? ` · ${fails} failed installs` : ""}`,
    };
  });
  add("disk", "Disk", "overview", d.disk, () => {
    const free = (d.disk as Extract<Dashboard["disk"], object>).free_bytes;
    return {
      level: free != null && free < MIN_FREE_BYTES ? "warn" : "ok",
      value: `${formatBytes(free, true)} free`,
    };
  });
  add("errors", "Errors and outages", "overview", d.errors, () => {
    const e = d.errors as Extract<Dashboard["errors"], { outages: unknown }>;
    const recent = e.last_outage_age_s != null && e.last_outage_age_s < RECENT_OUTAGE_S;
    return {
      level: recent ? "bad" : e.restarts_24h > 0 || e.crashes.length > 0 ? "warn" : "ok",
      value: `${e.server_log_errors.length} log errors · ${e.restarts_24h} restarts (24 h)`,
    };
  });
  return tiles;
}

export interface SeriesRow {
  key: keyof Omit<TimelinePoint, "t">;
  label: string;
  /** Floor of the y axis, so a quiet series reads flat. */
  floor: number;
  points: SparkPoint[];
}

const SERIES: { key: SeriesRow["key"]; label: string; floor: number; scale?: number }[] = [
  { key: "rps", label: "Requests/s", floor: 5 },
  { key: "in_flight", label: "In flight", floor: 6 },
  { key: "pool", label: "Threads busy", floor: 8 },
  { key: "lag_ms", label: "Loop lag (ms)", floor: 50 },
  { key: "cpu", label: "CPU %", floor: 100 },
  { key: "swap", label: "Swap (MB)", floor: 100, scale: 1 << 20 },
];

/** One row per figure on the shared time axis (the points' own `t`). */
export function timelineSeries(points: TimelinePoint[]): SeriesRow[] {
  return SERIES.map((s) => ({
    key: s.key,
    label: s.label,
    floor: s.floor,
    points: points.map((p) => {
      const v = p[s.key];
      return { t: p.t, v: v == null ? null : v / (s.scale ?? 1) };
    }),
  }));
}
