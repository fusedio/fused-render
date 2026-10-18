// Wire types and fetchers for the Monitor page's dashboard tabs: the
// `GET /api/system/dashboard` payload (fused_render/server/dashboard.py), the
// request log (`/api/system/net/*`, fused_render/netlog.py), one process's
// detail and the Record 60s endpoints. A section that failed on the server is
// `{error}` instead of its shape, so every section type is `T | SectionError`.
import { getJson, mutateJson } from "@platform/lib/api";

export interface SectionError {
  error: string;
}
export type Section<T> = T | SectionError;

export function sectionOk<T>(s: Section<T> | undefined | null): s is T {
  return !!s && typeof s === "object" && !("error" in s);
}

export type Level = "ok" | "warn" | "bad";

export interface Reason {
  key: string;
  text: string;
  link: string;
  level: Level;
}
export interface Verdict {
  level: Level;
  headline: string;
  reasons: Reason[];
}

export interface NetRow {
  id: string | number;
  seq: number;
  t: number;
  method: string;
  route: string;
  path: string;
  status: number | null;
  server_ms: number | null;
  held_ms: number | null;
  bytes: number | null;
  source: string;
  client: string;
  outcome: string | null;
  self: boolean;
  /** Browser-side queue wait, when the page reported one. */
  queue_ms?: number | null;
}
export interface NetClient {
  client: string;
  in_flight: number;
  saturated_s: number;
  top: { source: string; route: string; count: number } | null;
}
export interface NetSummary {
  rps_10s: number;
  rps_60s: number;
  in_flight: NetClient[];
  in_flight_total: number;
  oldest_in_flight_s: number;
  errors_60s: number;
  long_held: NetRow[];
  browser_queue_p95_ms: number | null;
  /** Simultaneous-request stats per client over the last 60 s, busiest first. */
  concurrency: { client: string; peak: number; series: number[]; at_limit_s: number }[];
  main_client: string | null;
}
export interface NetGroup {
  source: string;
  route: string;
  count: number;
  per_min: number;
  p50_ms: number | null;
  p95_ms: number | null;
  errors: number;
  max_in_flight: number;
  queue_p95_ms: number | null;
  queue_max_ms: number | null;
}
export interface NetSocket {
  kind: string;
  source: string;
  open: number;
  msgs_per_s: number;
}
export interface NetRequests {
  rows: NetRow[];
  cursor: number;
}

export interface TimelinePoint {
  t: number;
  rps: number | null;
  in_flight: number | null;
  pool: number | null;
  lag_ms: number | null;
  cpu: number | null;
  swap: number | null;
}

export interface Dashboard {
  t: number;
  verdict: Verdict;
  net: Section<{ summary: NetSummary; groups: NetGroup[] }>;
  server: Section<{
    pool: { borrowed: number; total: number } | null;
    loop_lag: { current_ms: number | null; max_60s_ms: number | null };
    sockets: Record<string, number>;
  }>;
  memory: Section<{
    used: number | null;
    total: number | null;
    swap_used: number | null;
    swap_growth_10m_bytes: number | null;
    pressure: string | null;
    biggest: { pid: number; label: string; mem: number } | null;
  }>;
  cpu: Section<{
    cpu_pct: number | null;
    load: number[] | null;
    ncpu: number | null;
    thermal_limit: number | null;
    low_power: boolean | null;
  }>;
  claude: Section<{
    total: number;
    fused: number;
    other: number;
    mem_bytes: number;
    cpu_pct: number;
    top: { pid: number; label: string; mem: number; cpu: number }[];
  }>;
  calls: Section<{
    window_s: number;
    top_overhead: {
      entrypoint: string;
      count: number;
      run_p95_ms: number | null;
      overhead_p50_ms: number | null;
      overhead_p95_ms: number | null;
    }[];
  }>;
  index: Section<{
    /** Lane reads not recorded yet (or stale): unknown, NOT idle. */
    unavailable?: boolean;
    waiting?: number;
    abandoned?: number;
    scans?: { running: number; limit: number };
  }>;
  ai: Section<{ workers: { model: string; state: string; in_flight: number; rss: number | null }[] }>;
  jobs: Section<{
    running: number;
    stalled: number;
    queued: number;
    stalled_list: { id: string | null; title: string | null }[];
  }>;
  installs: Section<{ in_flight: unknown[]; failures: { key: string; error: string }[] }>;
  disk: Section<{
    free_bytes: number | null;
    log_home?: number;
    call_store?: number;
    index_db?: number;
  }>;
  errors: Section<{
    outages: { t_down?: number; t_up?: number }[];
    last_outage_age_s: number | null;
    server_log_errors: string[];
    crashes: { name: string; t: number }[];
    restarts_24h: number;
  }>;
  baseline: Section<{ load: number | null; swap_used: number | null; rss: number | null }>;
  timeline: Section<{
    points: TimelinePoint[];
    events: { t: number; kind: "outage" | "boot" }[];
  }>;
}

export interface ProcessDetail {
  pid: number;
  argv: string[];
  cwd: string | null;
  parents: { pid: number; label: string }[];
  owner: { kind: string; label: string; ref: Record<string, unknown> };
  venv: string | null;
  started_at: number | null;
  ports: unknown[];
}

export interface RecordStatus {
  recording: boolean;
  seconds_left: number;
  /** Absolute folder the recordings are saved in. */
  dir?: string;
  recordings: { name: string; bytes: number }[];
  already?: boolean;
}

export const getDashboard = (signal?: AbortSignal) =>
  getJson<Dashboard>("/api/system/dashboard", { signal });
export const getNetSummary = () => getJson<NetSummary>("/api/system/net/summary");
export const getNetGroups = (window = 60) =>
  getJson<{ window: number; groups: NetGroup[] }>(`/api/system/net/groups?window=${window}`);
export const getNetSockets = () => getJson<{ sockets: NetSocket[] }>("/api/system/net/sockets");

export interface NetFilters {
  source: string;
  route: string;
  status: string;
  minMs: string;
  openOnly: boolean;
  own: boolean;
}
export function netRequestsUrl(since: number, f: NetFilters): string {
  const q = new URLSearchParams({ since: String(since) });
  if (f.source) q.set("source", f.source);
  if (f.route) q.set("route", f.route);
  if (f.status) q.set("status", f.status);
  if (f.minMs && Number.isFinite(Number(f.minMs))) q.set("min_ms", f.minMs);
  if (f.openOnly) q.set("open", "1");
  if (f.own) q.set("own", "1");
  return `/api/system/net/requests?${q}`;
}
export const getNetRequests = (since: number, f: NetFilters) =>
  getJson<NetRequests>(netRequestsUrl(since, f));

export const getProcessDetail = (pid: number) =>
  getJson<ProcessDetail>(`/api/system/activity/detail?pid=${pid}`);
export const startRecord = () => mutateJson<RecordStatus>("POST", "/api/system/record");
export const getRecordStatus = () => getJson<RecordStatus>("/api/system/record");
