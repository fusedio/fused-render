// The Monitor dashboard tabs' pure rules: URL params, the connection-slots
// strip, the live request list's merge, and the Overview tiles with their
// colours. No React and no network (monitor-api.ts owns the wire), so the
// suite can pin each rule directly. Tile thresholds mirror
// fused_render/sysmon/verdict.py: a tile is red or amber exactly when the
// verdict would name it, so the banner and the grid never disagree.
import { formatBytes } from "@platform/lib/sysmon";
import {
  sectionOk,
  type SectionError,
  type Dashboard,
  type Level,
  type NetRow,
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

/** The params a verdict reason's `/monitor?...` link (or any monitorUrl)
 *  names, so a click switches tab and filters in place rather than reloading. */
export function paramsFromLink(link: string): MonitorParams {
  const i = link.indexOf("?");
  return parseMonitorParams(i < 0 ? "" : link.slice(i));
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

/** "Claude CLIs: 3 (fused-render 2 · other 1) · 1.4 GB" — the Processes tab's
 *  summary line and the chip popover's. */
export function claudeLine(c: {
  total: number;
  fused: number;
  other: number;
  mem_bytes: number;
}): string {
  const split = c.other > 0 ? ` (fused-render ${c.fused} · other ${c.other})` : "";
  return `Claude CLIs: ${c.total}${split} · ${formatBytes(c.mem_bytes, true)}`;
}

export type TileLevel = Level | "unavailable";
export interface Tile {
  key: string;
  title: string;
  level: TileLevel;
  value: string;
  /** The big figure and the small line under it (value stays the one-line form). */
  primary: string;
  /** False when the figure has no reading (section failed or the value is null);
   *  consumers fall back on this, never on the placeholder text in `primary`. */
  available: boolean;
  caption: string;
  /** Full text for a hover title when `primary` shows an abbreviation (a path's basename). */
  hint?: string;
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

/** Last segment of a filesystem path (either separator); a bare name is returned as is. */
export function basename(path: string): string {
  const trimmed = path.replace(/[\\/]+$/, "");
  const i = Math.max(trimmed.lastIndexOf("/"), trimmed.lastIndexOf("\\"));
  return i >= 0 ? trimmed.slice(i + 1) : trimmed;
}

export function overviewTiles(d: Dashboard): Tile[] {
  const tiles: Tile[] = [];
  const add = (
    key: string,
    title: string,
    tab: Tab,
    section: unknown,
    fn: () => { level: Level; value: string; baseline?: string; primary?: string; caption?: string; hint?: string; available?: boolean },
  ) => {
    const off = {
      key, title, tab, level: "unavailable" as const, value: UNAVAILABLE, primary: UNAVAILABLE,
      available: false, caption: "",
    };
    if (!sectionOk(section as object)) {
      tiles.push(off);
      return;
    }
    try {
      const r = fn();
      const [first, ...rest] = r.value.split(" · ");
      const caption = r.caption ?? [...rest, ...(r.baseline ? [r.baseline] : [])].join(" · ");
      tiles.push({ key, title, tab, ...r, primary: r.primary ?? first, available: r.available ?? true, caption });
    } catch {
      tiles.push(off);
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
      primary: `${s.rps_10s}/s`,
      available: s.rps_10s != null,
      caption: `${worst} in flight`,
    };
  });
  add("queue", "Browser queue", "network", d.net, () => {
    const q = net!.summary.browser_queue_p95_ms;
    return {
      level: q != null && q > QUEUE_P95_MS ? "warn" : "ok",
      value: q == null ? "no data" : `${ms(q)} p95`,
      primary: q == null ? "no data" : ms(q),
      available: q != null,
      caption: q == null ? "" : "p95 wait for a connection",
    };
  });
  add("server", "Server", "overview", d.server, () => {
    const s = d.server as Exclude<Dashboard["server"], SectionError>;
    const pool = s.pool;
    const lag = s.loop_lag?.current_ms;
    const busy = !!pool && pool.total > 0 && pool.borrowed / pool.total >= POOL_BUSY_FRAC;
    const lagging = lag != null && lag > LOOP_LAG_MS;
    return {
      level: busy || lagging ? "bad" : "ok",
      value: `${pool ? `${pool.borrowed}/${pool.total} threads` : "threads ?"} · lag ${ms(lag)}`,
      primary: `${ms(lag)} lag`,
      available: lag != null,
      caption: pool ? `${pool.borrowed}/${pool.total} threads busy` : "threads ?",
    };
  });
  add("memory", "Memory and swap", "processes", d.memory, () => {
    const m = d.memory as Exclude<Dashboard["memory"], SectionError>;
    const growth = m.swap_growth_10m_bytes ?? 0;
    const level: Level =
      m.pressure === "critical" ? "bad" : m.pressure === "warn" || growth > SWAP_GROWTH_BYTES ? "warn" : "ok";
    const base = sectionOk(d.baseline) ? d.baseline.swap_used : null;
    return {
      level,
      value: `${formatBytes(m.used, true)} / ${formatBytes(m.total, true)} · swap ${formatBytes(m.swap_used, true)}`,
      primary: `${formatBytes(m.used, true).replace(/ ([KMGT]?B)$/, (x, u) => (formatBytes(m.total, true).endsWith(u) ? "" : x))} / ${formatBytes(m.total, true)}`,
      baseline: base != null ? `usual ${formatBytes(base, true)}` : undefined,
    };
  });
  add("cpu", "CPU and thermal", "processes", d.cpu, () => {
    const c = d.cpu as Exclude<Dashboard["cpu"], SectionError>;
    const throttled = c.thermal_limit != null && c.thermal_limit < 100;
    const base = sectionOk(d.baseline) ? d.baseline.load : null;
    const load = c.load?.[0];
    return {
      level: throttled || c.low_power ? "warn" : "ok",
      available: c.cpu_pct != null,
      value: `${c.cpu_pct == null ? "-" : Math.round(c.cpu_pct)}%${load != null ? ` · load ${load.toFixed(1)}` : ""}${throttled ? ` · throttled ${c.thermal_limit}%` : ""}`,
      baseline: base != null ? `usual load ${base.toFixed(1)}` : undefined,
    };
  });
  add("claude", "Claude CLIs", "processes", d.claude, () => {
    const c = d.claude as Exclude<Dashboard["claude"], SectionError>;
    return {
      level: c.total > CLAUDE_MAX_COUNT || c.mem_bytes > CLAUDE_MAX_BYTES ? "warn" : "ok",
      value: `${c.total} · ${formatBytes(c.mem_bytes, true)}`,
    };
  });
  add("calls", "Python calls", "network", d.calls, () => {
    const c = d.calls as Exclude<Dashboard["calls"], SectionError>;
    const top = c.top_overhead[0];
    return {
      level: "ok",
      value: top ? `${basename(top.entrypoint)}: ${ms(top.overhead_p95_ms)} overhead` : "no calls",
      hint: top?.entrypoint,
    };
  });
  add("index", "Index", "overview", d.index, () => {
    const i = d.index as Exclude<Dashboard["index"], SectionError>;
    if (i.unavailable) {
      // lane state unknown: never render it as "0 waiting" (that reads as idle)
      return { level: "ok", value: "lanes ? · ? abandoned · ? scans" };
    }
    const bad = (i.waiting ?? 0) > 0 || (i.abandoned ?? 0) > 0;
    return {
      level: bad ? "warn" : "ok",
      value: `${i.waiting ?? 0} waiting · ${i.abandoned ?? 0} abandoned · ${i.scans?.running ?? 0} scans`,
    };
  });
  add("ai", "AI workers", "processes", d.ai, () => {
    const a = d.ai as Exclude<Dashboard["ai"], SectionError>;
    const busy = a.workers.filter((w) => w.in_flight > 0).length;
    return { level: "ok", value: `${a.workers.length} resident · ${busy} busy` };
  });
  add("jobs", "Jobs and installs", "overview", d.jobs, () => {
    const j = d.jobs as Exclude<Dashboard["jobs"], SectionError>;
    const fails = sectionOk(d.installs) ? d.installs.failures.length : 0;
    return {
      level: j.stalled > 0 || fails > 0 ? "warn" : "ok",
      value: `${j.running} running · ${j.stalled} stalled${fails ? ` · ${fails} failed installs` : ""}`,
    };
  });
  add("disk", "Disk", "overview", d.disk, () => {
    const free = (d.disk as Exclude<Dashboard["disk"], SectionError>).free_bytes;
    return {
      level: free != null && free < MIN_FREE_BYTES ? "warn" : "ok",
      value: `${formatBytes(free, true)} free`,
    };
  });
  add("errors", "Errors and outages", "overview", d.errors, () => {
    const e = d.errors as Exclude<Dashboard["errors"], SectionError>;
    const recent = e.last_unexpected_outage_age_s != null && e.last_unexpected_outage_age_s < RECENT_OUTAGE_S;
    const nowS = Date.now() / 1000;
    const outages24 = e.outages.filter((o) => !o.expected && nowS - o.t_down < 86400).length;
    const logs = e.server_log_errors.length;
    const counts = [
      e.crashes_24h > 0 ? `${e.crashes_24h} ${e.crashes_24h === 1 ? "crash" : "crashes"}` : null,
      outages24 > 0 ? `${outages24} ${outages24 === 1 ? "outage" : "outages"}` : null,
      logs > 0 ? `${logs} log ${logs === 1 ? "error" : "errors"}` : null,
    ].filter((x): x is string => x !== null);
    return {
      primary: counts[0] ?? "No problems",
      caption: `${e.restarts_24h} restarts (24 h)`,
      level: recent ? "bad" : counts.length > 0 ? "warn" : "ok",
      value: `${counts.length ? counts.join(" · ") : "No problems"} · ${e.restarts_24h} restarts (24 h)`,
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

const FULL_WINDOW_S = 600;

/** Window to plot: the full 10 minutes, or just the history we have (min 60 s) so short plots fill the width. */
export function timelineWindowS(points: TimelinePoint[]): number {
  if (points.length < 2) return FULL_WINDOW_S;
  const span = points[points.length - 1].t - points[0].t;
  return span >= FULL_WINDOW_S ? FULL_WINDOW_S : Math.max(60, span);
}

export function timelineTitle(points: TimelinePoint[]): string {
  const w = timelineWindowS(points);
  return w >= FULL_WINDOW_S ? "Last 10 minutes" : `Last ${Math.max(1, Math.round(w / 60))} min`;
}

/** Smallest 1/2/5 x 10^n at or above `v`, never below `min`: a readable axis ceiling. */
export function niceCeil(v: number, min: number): number {
  const x = Math.max(v, min);
  const mag = Math.pow(10, Math.floor(Math.log10(x)));
  for (const m of [1, 2, 5, 10]) if (m * mag >= x) return m * mag;
  return 10 * mag;
}
