// The Monitor dashboard tabs' pure rules (monitor-net-lib.ts).
import { expect, test } from "bun:test";

import type { Dashboard, NetRow } from "@shell/monitor/monitor-api";
import { netRequestsUrl } from "@shell/monitor/monitor-api";
import {
  appendRows,
  claudeLine,
  monitorUrl,
  overviewTiles,
  paramsFromLink,
  parseMonitorParams,
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
    errors: { outages: [], last_unexpected_outage_age_s: null, server_log_errors: [], crashes: [], crashes_24h: 0, restarts_24h: 0 },
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
  expect(level(dash({ errors: { outages: [], last_unexpected_outage_age_s: 60, server_log_errors: [], crashes: [], crashes_24h: 0, restarts_24h: 0 } }), "errors")).toBe("bad");
  expect(level(dash({ memory: { used: 1, total: 2, swap_used: 0, swap_growth_10m_bytes: 0, pressure: "critical", biggest: null } }), "memory")).toBe("bad");
});

test("errors tile: restarts never raise the level; unexpected outage is bad; crash warns", () => {
  const base = { outages: [], last_unexpected_outage_age_s: null, server_log_errors: [], crashes: [], crashes_24h: 0, restarts_24h: 5 };
  const tile = (e: object) => overviewTiles(dash({ errors: { ...base, ...e } as never })).find((t) => t.key === "errors")!;
  expect(tile({}).level).toBe("ok");
  expect(tile({}).primary).toBe("No problems");
  expect(tile({}).caption).toBe("5 restarts (24 h)");
  expect(tile({ last_unexpected_outage_age_s: 60 }).level).toBe("bad");
  const c = tile({ crashes_24h: 2 });
  expect(c.level).toBe("warn");
  expect(c.primary).toBe("2 crashes");
  expect(tile({ server_log_errors: ["x"] }).primary).toBe("1 log error");
});

test("a failed section is unavailable in its own tile and nowhere else", () => {
  const tiles = overviewTiles(dash({ disk: { error: "boom" } }));
  const disk = tiles.find((t) => t.key === "disk");
  expect(disk?.level).toBe("unavailable");
  expect(disk?.value).toBe("unavailable");
  expect(tiles.filter((t) => t.level === "unavailable")).toHaveLength(1);
});

test("an index section with unavailable lane state shows ? not 0 waiting", () => {
  const tile = overviewTiles(dash({ index: { unavailable: true } })).find((t) => t.key === "index");
  expect(tile?.value).not.toMatch(/0 waiting|^0/);
  expect(tile?.value).toContain("?");
  expect(tile?.level).toBe("ok");
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

test("claudeLine names the split only when other > 0", () => {
  expect(claudeLine({ total: 3, fused: 2, other: 1, mem_bytes: 1.5 * (1 << 30) })).toBe(
    "Claude CLIs: 3 (fused-render 2 · other 1) · 1.5 GB",
  );
  expect(claudeLine({ total: 4, fused: 4, other: 0, mem_bytes: 1.5 * (1 << 30) })).toBe(
    "Claude CLIs: 4 · 1.5 GB",
  );
});

test("paramsFromLink reads a verdict reason's deep link", () => {
  expect(paramsFromLink("/monitor?tab=network&source=a%20b&route=%2Fapi%2Fx")).toEqual({
    tab: "network", source: "a b", route: "/api/x", pid: null,
  });
  expect(paramsFromLink("/monitor?tab=processes&pid=7").pid).toBe(7);
  expect(paramsFromLink("/monitor").tab).toBe("overview");
});
