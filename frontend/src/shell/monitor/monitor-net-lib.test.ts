// The Monitor dashboard tabs' pure rules (monitor-net-lib.ts).
import { expect, test } from "bun:test";

import type { Dashboard, NetRow } from "@shell/monitor/monitor-api";
import { netRequestsUrl } from "@shell/monitor/monitor-api";
import {
  appendRows,
  mainClient,
  monitorUrl,
  overviewTiles,
  parseMonitorParams,
  slotStrip,
  timelineSeries,
} from "@shell/monitor/monitor-net-lib";

function row(id: number, over: Partial<NetRow> = {}): NetRow {
  return {
    id,
    seq: id,
    t: 100 + id,
    method: "GET",
    route: "/api/x",
    path: "/api/x",
    status: 200,
    server_ms: 5,
    held_ms: 5,
    bytes: 10,
    source: "app",
    client: "c1",
    outcome: "ok",
    self: false,
    ...over,
  };
}

test("URL params: tab defaults to overview, junk tabs fall back, pid must be an integer", () => {
  expect(parseMonitorParams("")).toEqual({ tab: "overview", source: "", route: "", pid: null });
  expect(parseMonitorParams("?tab=network&source=my%20app&route=/api/x")).toEqual({
    tab: "network",
    source: "my app",
    route: "/api/x",
    pid: null,
  });
  expect(parseMonitorParams("?tab=bogus&pid=42").tab).toBe("overview");
  expect(parseMonitorParams("?tab=processes&pid=42").pid).toBe(42);
  expect(parseMonitorParams("?pid=abc").pid).toBeNull();
});

test("monitorUrl round-trips through parseMonitorParams", () => {
  const url = monitorUrl({ tab: "network", source: "my app", route: "/a/{id}" });
  expect(url.startsWith("/monitor?")).toBe(true);
  expect(parseMonitorParams(url.slice(url.indexOf("?")))).toEqual({
    tab: "network",
    source: "my app",
    route: "/a/{id}",
    pid: null,
  });
  expect(monitorUrl({ tab: "overview" })).toBe("/monitor");
});

test("slotStrip fills six boxes oldest first and counts the queued overflow", () => {
  const open = [row(3), row(1), row(2)];
  const s = slotStrip(open, 6);
  expect(s.boxes).toHaveLength(6);
  expect(s.boxes.map((b) => b && b.id)).toEqual([1, 2, 3, null, null, null]);
  expect(s.queued).toBe(0);
  const many = Array.from({ length: 9 }, (_, i) => row(i + 1));
  const m = slotStrip(many, 6);
  expect(m.boxes.every((b) => b !== null)).toBe(true);
  expect(m.queued).toBe(3);
});

test("mainClient is the client with the most in flight", () => {
  expect(mainClient(null)).toBeNull();
  expect(
    mainClient({
      in_flight: [
        { client: "a", in_flight: 2, saturated_s: 0, top: null },
        { client: "b", in_flight: 5, saturated_s: 0, top: null },
      ],
    } as never),
  ).toBe("b");
});

test("appendRows merges by id, keeps seq order and trims the oldest past the cap", () => {
  const a = [row(1, { status: null }), row(2)];
  const merged = appendRows(a, [row(1, { status: 200 }), row(3)], 10);
  expect(merged.map((r) => r.id)).toEqual([1, 2, 3]);
  expect(merged[0].status).toBe(200);
  const capped = appendRows(merged, [row(4), row(5)], 3);
  expect(capped.map((r) => r.id)).toEqual([3, 4, 5]);
});

test("netRequestsUrl carries only the filters that are set", () => {
  const f = { source: "", route: "", status: "", minMs: "", openOnly: false, own: false };
  expect(netRequestsUrl(7, f)).toBe("/api/system/net/requests?since=7");
  expect(
    netRequestsUrl(0, { ...f, source: "a b", minMs: "200", openOnly: true, own: true }),
  ).toBe("/api/system/net/requests?since=0&source=a+b&min_ms=200&open=1&own=1");
  expect(netRequestsUrl(0, { ...f, minMs: "abc" })).toBe("/api/system/net/requests?since=0");
});

const GB = 1 << 30;
function dash(over: Partial<Dashboard> = {}): Dashboard {
  return {
    t: 1,
    verdict: { level: "ok", headline: "Nothing looks wrong", reasons: [] },
    net: {
      summary: {
        rps_10s: 2,
        rps_60s: 1,
        in_flight: [{ client: "c", in_flight: 1, saturated_s: 0, top: null }],
        in_flight_total: 1,
        oldest_in_flight_s: 0.2,
        errors_60s: 0,
        long_held: [],
        browser_queue_p95_ms: 20,
      },
      groups: [],
    },
    server: { pool: { borrowed: 3, total: 40 }, loop_lag: { current_ms: 4, max_60s_ms: 9 }, sockets: {} },
    memory: {
      used: 8 * GB,
      total: 16 * GB,
      swap_used: 1 * GB,
      swap_growth_10m_bytes: 0,
      pressure: "normal",
      biggest: null,
    },
    cpu: { cpu_pct: 20, load: [1.2, 1, 1], ncpu: 8, thermal_limit: 100, low_power: false },
    claude: { total: 2, fused: 1, other: 1, mem_bytes: GB, cpu_pct: 1, top: [] },
    calls: { window_s: 600, top_overhead: [] },
    index: { waiting: 0, abandoned: 0, scans: { running: 0, limit: 2 } },
    ai: { workers: [] },
    jobs: { running: 0, stalled: 0, queued: 0, stalled_list: [] },
    installs: { in_flight: [], failures: [] },
    disk: { free_bytes: 100 * GB },
    errors: { outages: [], last_outage_age_s: null, server_log_errors: [], crashes: [], restarts_24h: 0 },
    baseline: { load: 1.0, swap_used: 1 * GB, rss: GB },
    timeline: { points: [], events: [] },
    ...over,
  } as Dashboard;
}

test("twelve tiles, all ok on a healthy dashboard, with baselines where known", () => {
  const tiles = overviewTiles(dash());
  expect(tiles.map((t) => t.key)).toEqual([
    "requests", "queue", "server", "memory", "cpu", "claude",
    "calls", "index", "ai", "jobs", "disk", "errors",
  ]);
  expect(tiles.every((t) => t.level === "ok")).toBe(true);
  expect(tiles.find((t) => t.key === "cpu")?.baseline).toBe("usual load 1.0");
});

test("tile colours follow the verdict thresholds", () => {
  const level = (d: Dashboard, key: string) => overviewTiles(d).find((t) => t.key === key)?.level;
  expect(level(dash({ server: { pool: { borrowed: 38, total: 40 }, loop_lag: { current_ms: 4, max_60s_ms: 9 }, sockets: {} } }), "server")).toBe("bad");
  expect(level(dash({ server: { pool: { borrowed: 3, total: 40 }, loop_lag: { current_ms: 300, max_60s_ms: 300 }, sockets: {} } }), "server")).toBe("bad");
  const n = dash().net as never as { summary: Record<string, unknown> };
  expect(level(dash({ net: { summary: { ...n.summary, browser_queue_p95_ms: 900 }, groups: [] } as never }), "queue")).toBe("warn");
  expect(level(dash({ net: { summary: { ...n.summary, in_flight: [{ client: "c", in_flight: 6, saturated_s: 3, top: null }] }, groups: [] } as never }), "requests")).toBe("bad");
  expect(level(dash({ disk: { free_bytes: 2 * GB } }), "disk")).toBe("warn");
  expect(level(dash({ claude: { total: 9, fused: 9, other: 0, mem_bytes: GB, cpu_pct: 0, top: [] } }), "claude")).toBe("warn");
  expect(level(dash({ errors: { outages: [], last_outage_age_s: 60, server_log_errors: [], crashes: [], restarts_24h: 0 } }), "errors")).toBe("bad");
  expect(level(dash({ memory: { used: 1, total: 2, swap_used: 0, swap_growth_10m_bytes: 0, pressure: "critical", biggest: null } }), "memory")).toBe("bad");
});

test("a failed section is unavailable in its own tile and nowhere else", () => {
  const tiles = overviewTiles(dash({ disk: { error: "boom" } }));
  const disk = tiles.find((t) => t.key === "disk");
  expect(disk?.level).toBe("unavailable");
  expect(disk?.value).toBe("unavailable");
  expect(tiles.filter((t) => t.level === "unavailable")).toHaveLength(1);
});

test("timelineSeries gives one row per figure and skips nulls", () => {
  const rows = timelineSeries([
    { t: 1, rps: 1, in_flight: 0, pool: 2, lag_ms: 3, cpu: 10, swap: 1 << 20 },
    { t: 2, rps: null, in_flight: 1, pool: 2, lag_ms: 3, cpu: 12, swap: 2 << 20 },
  ]);
  expect(rows.map((r) => r.key)).toEqual(["rps", "in_flight", "pool", "lag_ms", "cpu", "swap"]);
  expect(rows[0].points.map((p) => p.v)).toEqual([1, null]);
  expect(rows[5].points.map((p) => p.v)).toEqual([1, 2]);
});
