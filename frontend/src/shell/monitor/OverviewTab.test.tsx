// The Overview tab (and the tab shell) rendered against a fixed dashboard.
import { afterEach, expect, jest, test } from "bun:test";
import { act, create, type ReactTestInstance, type ReactTestRenderer } from "react-test-renderer";

import type { Dashboard } from "@shell/monitor/monitor-api";
import OverviewTab, { collapseRuns } from "@shell/monitor/OverviewTab";

const GB = 1 << 30;
const DASH = {
  t: 1000,
  verdict: {
    level: "bad",
    headline: "All 6 browser connections held for 9s+ by page-a x /api/x",
    reasons: [
      { key: "connections", level: "bad", text: "All 6 browser connections held for 9s+ by page-a x /api/x", link: "/monitor?tab=network&source=page-a&route=%2Fapi%2Fx" },
      { key: "claude", level: "warn", text: "9 Claude CLIs open using 5.0 GB", link: "/monitor?tab=processes" },
    ],
  },
  net: { summary: { rps_10s: 4, rps_60s: 3, in_flight: [{ client: "c", in_flight: 6, saturated_s: 9, top: null }], in_flight_total: 6, oldest_in_flight_s: 9, errors_60s: 0, long_held: [], browser_queue_p95_ms: 700 }, groups: [] },
  server: { pool: { borrowed: 3, total: 40 }, loop_lag: { current_ms: 4, max_60s_ms: 9 }, sockets: {} },
  memory: { used: 8 * GB, total: 16 * GB, swap_used: GB, swap_growth_10m_bytes: 0, pressure: "normal", biggest: null },
  cpu: { cpu_pct: 20, load: [1.2, 1, 1], ncpu: 8, thermal_limit: 100, low_power: false },
  claude: { total: 9, fused: 5, other: 4, mem_bytes: 5 * GB, cpu_pct: 1, top: [] },
  calls: { window_s: 600, top_overhead: [{ entrypoint: "/x/slow.py", count: 3, run_p95_ms: 50, overhead_p50_ms: 100, overhead_p95_ms: 800 }] },
  index: { waiting: 0, abandoned: 0, scans: { running: 0, limit: 2 } },
  ai: { workers: [] },
  jobs: { running: 1, stalled: 1, queued: 0, stalled_list: [{ id: "j1", title: "Stuck export" }] },
  installs: { in_flight: [], failures: [] },
  disk: { error: "OSError: boom" },
  errors: { outages: [], last_unexpected_outage_age_s: null, server_log_errors: ["2026 ERROR [x] bad thing"], crashes: [], crashes_24h: 0, restarts_24h: 0 },
  baseline: { load: 1.0, swap_used: GB, rss: GB },
  timeline: {
    points: [1, 2, 3].map((i) => ({ t: 990 + i, rps: i, in_flight: i, pool: 1, lag_ms: 1, cpu: 10, swap: GB })),
    events: [{ t: 992, kind: "outage" }],
  },
} as unknown as Dashboard;

let renderer: ReactTestRenderer | null = null;
afterEach(() => {
  act(() => renderer?.unmount());
  renderer = null;
});
const text = (n: ReactTestInstance | string): string =>
  typeof n === "string" ? n : n.children.map((c) => text(c as ReactTestInstance | string)).join("");
const byClass = (r: ReactTestRenderer, c: string) =>
  r.root.findAll((n) => typeof n.type === "string" && n.props.className?.split(" ").includes(c));

const btnOf = (r: ReactTestRenderer, label: string) =>
  r.root.find((n) => n.type === "button" && text(n).startsWith(label));

async function mount(onNavigate: (l: string) => void = () => {}, extra: Record<string, unknown> = {}) {
  await act(async () => {
    renderer = create(
      <OverviewTab
        load={async () => DASH}
        pollMs={1e9}
        onNavigate={onNavigate}
        recordApi={{
          start: async () => ({ recording: true, seconds_left: 60, recordings: [] }),
          status: async () => ({ recording: false, seconds_left: 0, dir: "/logs/perf-recordings", recordings: [{ name: "a.json", bytes: 1 }] }),
        }}
        {...extra}
      />,
    );
  });
  await act(async () => {
    for (let i = 0; i < 8; i++) await Promise.resolve();
  });
  return renderer!;
}

test("no banner box; a non-ok verdict lists its reasons as plain links that navigate in place", async () => {
  const seen: string[] = [];
  const r = await mount((l) => seen.push(l));
  expect(byClass(r, "monitor-verdict")).toHaveLength(0);
  expect(text(r.root)).not.toContain("Nothing looks wrong");
  const list = byClass(r, "monitor-reasons")[0];
  const links = list.findAll((n) => n.type === "a");
  expect(links).toHaveLength(2);
  expect(byClass(r, "monitor-reason-dot")).toHaveLength(2);
  expect(list.findAll((n) => n.type === "li").map((n) => n.props.className)).toEqual(["is-bad", "is-warn"]);
  let prevented = false;
  links[0].props.onClick({ button: 0, preventDefault: () => (prevented = true) });
  expect(prevented).toBe(true);
  expect(seen).toEqual(["/monitor?tab=network&source=page-a&route=%2Fapi%2Fx"]);
  seen.length = 0;
  links[1].props.onClick({ button: 0, metaKey: true, preventDefault: () => {} });
  expect(seen).toEqual([]);
});

test("an ok verdict renders nothing, and Record/Copy sit on the window heading's row", async () => {
  const dash = { ...DASH, verdict: { level: "ok", headline: "Nothing looks wrong", reasons: [] } } as unknown as Dashboard;
  const r = await mount(() => {}, { load: async () => dash });
  expect(byClass(r, "monitor-reasons")).toHaveLength(0);
  expect(text(r.root)).not.toContain("Nothing looks wrong");
  const row = byClass(r, "monitor-overview-window")[0];
  expect(text(row)).toContain("Last 1 min");
  expect(text(row)).toContain("Record 60s");
  expect(text(row)).toContain("Copy report");
});

test("the errors tile toggles an inline panel of recent errors", async () => {
  const dash = {
    ...DASH,
    errors: { ...DASH.errors, restarts_24h: 2, server_log_errors: ["2026 ERROR [x] bad thing", "2026 ERROR [x] bad thing"] },
  } as unknown as Dashboard;
  const r = await mount(() => {}, { load: async () => dash });
  const tile = () => r.root.find((n) => n.type === "button" && n.props["data-tile"] === "errors");
  expect(tile().props["aria-expanded"]).toBe(false);
  expect(byClass(r, "monitor-errors-panel")).toHaveLength(0);
  await act(async () => tile().props.onClick());
  expect(tile().props["aria-expanded"]).toBe(true);
  expect(text(byClass(r, "monitor-errors-panel")[0])).toContain("2026 ERROR [x] bad thing ×2");
  await act(async () => tile().props.onClick());
  expect(byClass(r, "monitor-errors-panel")).toHaveLength(0);
});

test("errors panel: hangs, crashes and crash links shown, restarts only counted", async () => {
  const now = Date.now() / 1000;
  const dash = {
    ...DASH,
    errors: {
      ...DASH.errors,
      server_log_errors: [],
      crashes_24h: 1,
      crashes: [{ name: "server-1.log", bytes: 2048, t: now - 120, path: "/Users/x/Logs/crash/server-1.log" }],
      outages: [
        { t_down: now - 600, t_up: now - 540, duration_s: 60, restarted: false, crashed: false, expected: false },
        { t_down: now - 900, t_up: now - 880, duration_s: 20, restarted: true, crashed: false, expected: true },
        { t_down: now - 300, t_up: now - 240, duration_s: 60, restarted: true, crashed: true, expected: false },
      ],
    },
  } as unknown as Dashboard;
  const r = await mount(() => {}, { load: async () => dash });
  const tile = r.root.find((n) => n.type === "button" && n.props["data-tile"] === "errors");
  await act(async () => tile.props.onClick());
  const panel = text(byClass(r, "monitor-errors-panel")[0]);
  expect(panel).toContain("not responding for 1 min (server stayed up)");
  expect(panel).toContain("crashed, down 1 min");
  expect(panel).not.toContain("still down");
  expect(byClass(r, "monitor-errors-panel")[0].findAll((n) => n.type === "li" && typeof n.props.title === "string" && n.props.title.includes("not responding")).length).toBe(1);
  expect(panel).toContain("1 restarts hidden");
  expect(panel).toContain("server-1.log · 2 KB");
  const link = byClass(r, "monitor-errors-panel")[0].find((n) => n.type === "a");
  expect(decodeURIComponent(link.props.href)).toContain("Users/x/Logs/crash/server-1.log");
});

test("errors panel is empty-stated when nothing is wrong", async () => {
  const dash = { ...DASH, errors: { ...DASH.errors, server_log_errors: [] } } as unknown as Dashboard;
  const r = await mount(() => {}, { load: async () => dash });
  await act(async () => btnOf(r, "Show all checks").props.onClick());
  const tile = r.root.find((n) => n.type === "button" && n.props["data-tile"] === "errors");
  await act(async () => tile.props.onClick());
  expect(text(byClass(r, "monitor-errors-panel")[0])).toContain("Nothing to report");
});

test("four chart cards with figures; alerting tiles sit above them; the rest behind Show all checks", async () => {
  const r = await mount();
  const cards = byClass(r, "monitor-overview-card");
  expect(cards.map((c) => c.props["data-card"])).toEqual(["requests", "queue", "server", "cpu"]);
  expect(byClass(r, "monitor-overview-cards")).toHaveLength(1);
  expect(text(byClass(r, "monitor-overview-window")[0])).toStartWith("Last 1 min");
  const fig = (i: number) => text(cards[i].findAll((n) => n.props.className === "monitor-card-figure")[0]);
  expect(fig(0)).toBe("4/s");
  expect(fig(1)).toBe("700 ms wait");
  expect(fig(2)).toBe("4 ms lag");
  expect(fig(3)).toBe("20%");
  expect(text(cards[0])).toContain("6 in flight");
  expect(text(cards[1])).toContain("limit 6 per host");
  expect(text(cards[2])).toContain("3/40 threads busy");
  expect(text(cards[3])).toContain("8.0 / 16.0 GB · swap 1.0 GB");
  expect(text(cards[3])).toContain("swap");
  expect(text(cards[1])).toContain("6 limit");
  // charts draw from the timeline
  expect(cards.every((c) => c.findAll((n) => n.props.className === "monitor-chart").length === 1)).toBe(true);
  // The fixture's claude, jobs and errors tiles are warn: the borderless stat row.
  const keys = () => byClass(r, "monitor-tile").map((t) => t.props["data-tile"]);
  expect(keys()).toEqual(["claude", "jobs", "errors"]);
  expect(byClass(r, "monitor-lists")).toHaveLength(0);
  expect(text(r.root)).not.toContain("Stuck export");
  await act(async () => btnOf(r, "Show all checks (4)").props.onClick());
  expect(keys()).toHaveLength(7);
  const tiles = byClass(r, "monitor-tile");
  const disk = tiles.find((t) => t.props["data-tile"] === "disk")!;
  expect(disk.props.className).toContain("is-unavailable");
  expect(text(disk)).toContain("unavailable");
  await act(async () => btnOf(r, "Hide checks").props.onClick());
  expect(keys()).toHaveLength(3);
});

test("a warn/bad card is tinted and a plain click on it navigates", async () => {
  const seen: string[] = [];
  const r = await mount((l) => seen.push(l));
  const cards = byClass(r, "monitor-overview-card");
  expect(cards[0].props.className).toContain("is-bad");
  expect(cards[1].props.className).toContain("is-warn");
  expect(cards[2].props.className).not.toContain("is-");
  cards[0].props.onClick({ button: 0, preventDefault: () => {} });
  expect(seen).toEqual(["/monitor?tab=network"]);
  seen.length = 0;
  cards[0].props.onClick({ button: 0, metaKey: true, preventDefault: () => {} });
  expect(seen).toEqual([]);
});

test("a warn/bad tile outside the headline cards shows without expanding", async () => {
  const r = await mount(() => {}, {
    load: async () => ({ ...DASH, index: { waiting: 2, abandoned: 0, scans: { running: 0, limit: 2 } } }) as Dashboard,
  });
  expect(byClass(r, "monitor-tile").map((t) => t.props["data-tile"])).toEqual(["claude", "index", "jobs", "errors"]);
  expect(btnOf(r, "Show all checks")).toBeTruthy();
});

test("lists fill once expanded", async () => {
  const r = await mount();
  await act(async () => btnOf(r, "Show all checks").props.onClick());
  expect(text(r.root)).toContain("Stuck export");
  expect(text(r.root)).toContain("/x/slow.py");
  expect(text(r.root)).toContain("bad thing");
});

test("collapseRuns merges identical consecutive labels", () => {
  expect(collapseRuns(["a", "a", "a", "b", "a"])).toEqual([
    { label: "a", n: 3 },
    { label: "b", n: 1 },
    { label: "a", n: 1 },
  ]);
});

test("Record 60s counts down, Copy report copies the dashboard JSON, the saved file is linked", async () => {
  let copied = "";
  const r = await mount(() => {}, { copy: (t: string) => void (copied = t) });
  const btn = (label: string) => r.root.find((n) => n.type === "button" && text(n).startsWith(label));
  expect(text(r.root)).toContain("Saved: a.json");
  const link = r.root.find((n) => n.props.className === "monitor-recording-link");
  expect(link.props.href).toContain("perf-recordings");
  await act(async () => btn("Record 60s").props.onClick());
  expect(text(btn("Recording")).includes("60s")).toBe(true);
  await act(async () => btn("Copy report").props.onClick());
  expect(JSON.parse(copied).verdict.level).toBe("bad");
});

test("Copy report says 'Copy failed' when the clipboard write rejects", async () => {
  const r = await mount(() => {}, { copy: () => Promise.reject(new Error("denied")) });
  await act(async () => btnOf(r, "Copy report").props.onClick());
  expect(text(btnOf(r, "Copy failed"))).toBe("Copy failed");
});

test("Copy report says 'Copy failed' when navigator.clipboard is undefined", async () => {
  const had = Object.getOwnPropertyDescriptor(globalThis.navigator, "clipboard");
  Object.defineProperty(globalThis.navigator, "clipboard", { value: undefined, configurable: true });
  try {
    const r = await mount(); // the default copy reads navigator.clipboard
    await act(async () => btnOf(r, "Copy report").props.onClick());
    expect(text(btnOf(r, "Copy failed"))).toBe("Copy failed");
  } finally {
    if (had) Object.defineProperty(globalThis.navigator, "clipboard", had);
    else delete (globalThis.navigator as unknown as Record<string, unknown>).clipboard;
  }
});

test("the 'Copied' reset timer is cleared on unmount", async () => {
  jest.useFakeTimers();
  try {
    const r = await mount(() => {}, { copy: () => {} });
    await act(async () => btnOf(r, "Copy report").props.onClick());
    expect(text(btnOf(r, "Copied"))).toBe("Copied");
    expect(jest.getTimerCount()).toBeGreaterThan(0);
    act(() => renderer?.unmount());
    renderer = null;
    expect(jest.getTimerCount()).toBe(0);
  } finally {
    jest.useRealTimers();
  }
});

test("figures fall back to the latest timeline value when the live sample is missing; title tracks the plotted window", async () => {
  const pts = (n: number, step: number) => Array.from({ length: n }, (_, i) => ({ t: 1000 - (n - 1 - i) * step, rps: 2.4, in_flight: 1, pool: 1, lag_ms: 7.4, cpu: i === n - 1 ? null : 19.4, swap: GB }));
  const dash = { ...DASH, cpu: { cpu_pct: null, load: [1, 1, 1], ncpu: 8, thermal_limit: 100, low_power: false }, timeline: { points: pts(11, 60), events: [] } } as unknown as Dashboard;
  const r = await mount(() => {}, { load: async () => dash });
  const cards = byClass(r, "monitor-overview-card");
  const fig = (i: number) => text(cards[i].findAll((n) => n.props.className === "monitor-card-figure")[0]);
  expect(fig(3)).toBe("19%");
  expect(text(byClass(r, "monitor-overview-window")[0].find((n) => n.type === "h2"))).toBe("Last 10 minutes");
});

test("a null loop-lag reading falls back to the latest timeline value", async () => {
  const dash = {
    ...DASH,
    server: { ...(DASH.server as object), loop_lag: { current_ms: null, max_60s_ms: null } },
  } as unknown as Dashboard;
  const r = await mount(() => {}, { load: async () => dash });
  const card = byClass(r, "monitor-overview-card").find((c) => c.props["data-card"] === "server")!;
  expect(text(card.findAll((n) => n.props.className === "monitor-card-figure")[0])).toBe("1 ms lag");
});
