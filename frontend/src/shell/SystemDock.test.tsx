// The System chip's presentational rules, rendered through the pure
// `SystemCardView` with a fixed payload (no poll, no network).
import { expect, test } from "bun:test";
import { act, create, type ReactTestRendererJSON } from "react-test-renderer";

import { MONITOR_PATH, chipLabel, hostLine, SystemCardView, topProcs, useOpenDashboard } from "@shell/SystemDock";
import type { SystemActivity, SystemProc } from "@platform/lib/sysmon";

const GB = 1024 ** 3;
const MB = 1024 ** 2;

function proc(pid: number, label: string, cpuPct: number | null, memBytes: number): SystemProc {
  return { pid, ppid: 1, kind: pid === 1 ? "app" : "other", label, cpuPct, memBytes, startedAt: null };
}

const PAYLOAD: SystemActivity = {
  supported: true,
  host: {
    cpuPct: 34.2,
    cpuUser: 20,
    cpuSystem: 14.2,
    ncpu: 10,
    load: [2, 2, 2],
    memTotal: 16 * GB,
    memUsed: 10.8 * GB,
    memApp: 6 * GB,
    memWired: 2 * GB,
    memCompressed: 2.8 * GB,
  },
  procs: [
    proc(1, "fused-render", 4.0, 1.02 * GB),
    proc(2, "Claude: fix the bug", 12.3, 194.3 * MB),
    proc(3, "Model: qwen", 40.1, 3 * GB),
    proc(4, "Engine: map", 0.5, 80 * MB),
    proc(5, "Terminal", 0.1, 5 * MB),
    proc(6, "Run: Python", 2.0, 60 * MB),
    proc(7, "sleep", null, 1 * MB),
  ],
  history: [
    { t: 100, cpuPct: 30, memUsed: 10 * GB, appCpuPct: 50, appMemBytes: 4 * GB },
    { t: 101, cpuPct: 31, memUsed: 10 * GB, appCpuPct: 59, appMemBytes: 4 * GB },
    { t: 102, cpuPct: 34, memUsed: 10 * GB, appCpuPct: 59.5, appMemBytes: 4.4 * GB },
  ],
  totals: { cpuPct: 59.5, memBytes: 1.4 * GB },
};

function texts(node: ReactTestRendererJSON | string | null): string {
  if (node === null) return "";
  if (typeof node === "string") return node;
  return (node.children ?? []).map((c) => texts(c as ReactTestRendererJSON | string)).join("");
}

function byClass(node: ReactTestRendererJSON | null, cls: string): ReactTestRendererJSON[] {
  if (!node || typeof node === "string") return [];
  const own =
    typeof node.props?.className === "string" && node.props.className.split(" ").includes(cls)
      ? [node]
      : [];
  return own.concat(...(node.children ?? []).map((c) => byClass(c as ReactTestRendererJSON, cls)));
}

test("chip shows fused-render's own totals, not the machine's", () => {
  expect(chipLabel(PAYLOAD)).toBe("CPU 60% · 1.4 GB");
  expect(chipLabel(null)).toBe("System");
  expect(chipLabel({ ...PAYLOAD, totals: { cpuPct: null, memBytes: 512 * MB } })).toBe(
    "CPU – · 512.0 MB",
  );
});

test("popover header carries the whole-machine figures", () => {
  expect(hostLine(PAYLOAD, "This Mac")).toBe("This Mac: CPU 34% · Memory 10.8 of 16 GB");
  expect(hostLine(null, "This Mac")).toBe("This Mac");
});

test("collapsed renders only the chip, with the totals label", () => {
  const tree = create(
    <SystemCardView data={PAYLOAD} collapsed onToggle={() => {}} onOpenMonitor={() => {}} />,
  ).toJSON() as ReactTestRendererJSON;
  expect(texts(tree)).toContain("CPU 60% · 1.4 GB");
  expect(byClass(tree, "dl-panel")).toHaveLength(0);
});

test("open popover lists the top 5 by CPU with Activity Monitor style figures", () => {
  const tree = create(
    <SystemCardView data={PAYLOAD} collapsed={false} onToggle={() => {}} onOpenMonitor={() => {}} />,
  ).toJSON() as ReactTestRendererJSON;
  const rows = byClass(tree, "sys-row");
  expect(rows).toHaveLength(5);
  expect(rows.map((r) => texts(byClass(r, "sys-name")[0]))).toEqual([
    "Model: qwen",
    "Claude: fix the bug",
    "fused-render",
    "Run: Python",
    "Engine: map",
  ]);
  expect(texts(rows[0])).toContain("40.1%");
  expect(texts(rows[0])).toContain("3.00 GB");
  expect(texts(rows[1])).toContain("194.3 MB");
  expect(texts(rows[2])).toContain("1.02 GB");
  expect(byClass(tree, "sys-spark")).toHaveLength(1);
  expect(texts(tree)).toContain("Open Monitor");
});

test("Open Monitor links to the /monitor page and calls through on a plain click", () => {
  let opened = 0;
  const r = create(
    <SystemCardView
      data={PAYLOAD}
      collapsed={false}
      onToggle={() => {}}
      onOpenMonitor={() => opened++}
    />,
  );
  const link = r.root.findByProps({ className: "sys-link" });
  expect(link.type).toBe("a");
  expect(link.props.href).toBe("/monitor");
  expect(MONITOR_PATH).toBe("/monitor");
  let prevented = false;
  link.props.onClick({ button: 0, preventDefault: () => (prevented = true) });
  expect([opened, prevented]).toEqual([1, true]);
  // Cmd-click is the browser's (a new window), not an in-app hop.
  link.props.onClick({ button: 0, metaKey: true, preventDefault: () => {} });
  expect(opened).toBe(1);
  // One link only: nothing in the popover leads to the status bar's Activity panel.
  expect(r.root.findAllByProps({ className: "sys-link" })).toHaveLength(1);
  expect(texts(r.toJSON() as ReactTestRendererJSON)).not.toContain("Panel");
});

test("the sparkline draws only the last 60 s of a 120 s history", () => {
  const long = Array.from({ length: 120 }, (_, i) => ({
    t: 1000 + i,
    cpuPct: 10,
    memUsed: GB,
    appCpuPct: i < 60 ? 90 : 10,
    appMemBytes: GB,
  }));
  const tree = create(
    <SystemCardView
      data={{ ...PAYLOAD, history: long }}
      collapsed={false}
      onToggle={() => {}}
      onOpenMonitor={() => {}}
    />,
  ).toJSON() as ReactTestRendererJSON;
  const line = byClass(tree, "sys-spark-line")[0].props.d as string;
  // 61 points (t = 1059..1119): the minute before is cut, not squashed to x=0.
  expect(line.split(/[ML]/).filter(Boolean)).toHaveLength(61);
  expect(line.startsWith("M0.0,")).toBe(true);
});

test("no chip until the first payload says the platform is supported", () => {
  const view = (data: SystemActivity | null) =>
    create(
      <SystemCardView data={data} collapsed onToggle={() => {}} onOpenMonitor={() => {}} />,
    ).toJSON();
  // Loading: nothing painted, so an unsupported platform never flashes "System".
  expect(view(null)).toBeNull();
  expect(view({ ...PAYLOAD, supported: false })).toBeNull();
  expect(texts(view(PAYLOAD) as ReactTestRendererJSON)).toContain("CPU 60% · 1.4 GB");
});

test("topProcs orders unknown CPU last, memory breaking ties", () => {
  const order = topProcs(
    [proc(1, "a", null, 10), proc(2, "b", null, 20), proc(3, "c", 0, 1)],
    3,
  ).map((p) => p.label);
  expect(order).toEqual(["c", "b", "a"]);
});

// ---- verdict, Claude line, timeline, record --------------------------------------------------

import type { Dashboard } from "@shell/monitor/monitor-api";

const VERDICT_DASH = {
  verdict: {
    level: "bad",
    headline: "All 6 browser connections held",
    reasons: [{ key: "connections", level: "bad", text: "All 6 browser connections held", link: "/monitor?tab=network&source=a" }],
  },
  net: { summary: { rps_10s: 2, rps_60s: 1, in_flight: [], in_flight_total: 0, oldest_in_flight_s: 0, errors_60s: 0, long_held: [], browser_queue_p95_ms: null }, groups: [] },
  server: { pool: { borrowed: 1, total: 40 }, loop_lag: { current_ms: 1, max_60s_ms: 2 }, sockets: {} },
  memory: { used: GB, total: 2 * GB, swap_used: 0, swap_growth_10m_bytes: 0, pressure: "normal", biggest: null },
  cpu: { cpu_pct: 10, load: [1, 1, 1], ncpu: 8, thermal_limit: 100, low_power: false },
  baseline: { load: 0.8, swap_used: 0, rss: GB },
  timeline: {
    points: [1, 2, 3].map((i) => ({ t: 100 + i, rps: i, in_flight: 0, pool: 1, lag_ms: 1, cpu: 10, swap: 0 })),
    events: [],
  },
} as unknown as Dashboard;

test("a warn/bad verdict paints the chip dot; ok keeps the slot but hides it", () => {
  const view = (verdict: SystemActivity["verdict"]) =>
    create(
      <SystemCardView data={{ ...PAYLOAD, verdict }} collapsed onToggle={() => {}} onOpenMonitor={() => {}} />,
    ).toJSON() as ReactTestRendererJSON;
  expect(byClass(view({ level: "bad", headline: "x" }), "sc-dot")[0].props.className).toContain("is-bad");
  expect(byClass(view({ level: "warn", headline: "x" }), "sc-dot")[0].props.className).toContain("is-warn");
  expect(byClass(view({ level: "ok", headline: "" }), "sc-dot")[0].props.className).toContain("is-none");
  // The label is untouched: the dot is never part of the chip text.
  expect(texts(view({ level: "bad", headline: "x" }))).toContain("CPU 60% · 1.4 GB");
});

test("the popover shows the headline, reason links, Claude line and the three stats", () => {
  const opened: string[] = [];
  let started = 0;
  const r = create(
    <SystemCardView
      data={{ ...PAYLOAD, claude: { total: 3, fused: 2, other: 1, mem_bytes: 1.5 * GB, cpu_pct: 1, top: [] }, verdict: { level: "bad", headline: "All 6 browser connections held" } }}
      collapsed={false}
      onToggle={() => {}}
      onOpenMonitor={() => {}}
      dash={VERDICT_DASH}
      onOpenLink={(l) => opened.push(l)}
      record={{ recording: false, secondsLeft: 0, onStart: () => started++ }}
    />,
  );
  const tree = r.toJSON() as ReactTestRendererJSON;
  expect(texts(byClass(tree, "sys-verdict")[0])).toContain("All 6 browser connections held");
  expect(texts(byClass(tree, "sys-claude")[0])).toBe("Claude CLIs: 3 (fused-render 2 · other 1) · 1.5 GB");
  expect(byClass(tree, "monitor-tl-row").length).toBe(0);
  expect(byClass(tree, "sys-keys").length).toBe(0);
  const stats = byClass(tree, "sys-stats")[0];
  expect(texts(stats)).toContain("Requests");
  expect(texts(stats)).toContain("2/s");
  expect(texts(stats)).toContain("Browser wait");
  expect(texts(stats)).toContain("Server lag");
  expect(texts(stats)).toContain("1 ms");
  const reason = r.root.findByProps({ className: "sys-reason" });
  reason.props.onClick({ button: 0, preventDefault() {} });
  expect(opened).toEqual(["/monitor?tab=network&source=a"]);
  r.root.findByProps({ className: "sys-record" }).props.onClick();
  expect(started).toBe(1);
  // The pinned single "Open Monitor" link is still the only sys-link.
  expect(r.root.findAllByProps({ className: "sys-link" })).toHaveLength(1);
});

test("the chip's accessible name carries a warn/bad verdict, not ok or unknown", () => {
  const label = (verdict: SystemActivity["verdict"]) =>
    create(
      <SystemCardView data={{ ...PAYLOAD, verdict }} collapsed onToggle={() => {}} onOpenMonitor={() => {}} />,
    ).root.find((n) => n.type === "button").props["aria-label"] as string;
  expect(label({ level: "bad", headline: "All 6 connections held" })).toBe(
    "fused-render is using CPU 60% · 1.4 GB · All 6 connections held",
  );
  expect(label({ level: "warn", headline: "Swap growing" })).toContain("· Swap growing");
  expect(label({ level: "ok", headline: "Nothing wrong" })).toBe("fused-render is using CPU 60% · 1.4 GB");
  expect(label({ level: "unknown", headline: "" })).toBe("fused-render is using CPU 60% · 1.4 GB");
});

test("an unknown verdict paints no dot", () => {
  const tree = create(
    <SystemCardView
      data={{ ...PAYLOAD, verdict: { level: "unknown", headline: "" } }}
      collapsed
      onToggle={() => {}}
      onOpenMonitor={() => {}}
    />,
  ).toJSON() as ReactTestRendererJSON;
  expect(byClass(tree, "sc-dot")[0].props.className).toContain("is-none");
});

test("the dashboard is dropped when the popover closes and not shown stale on reopen", async () => {
  let n = 0;
  const load = async () => ({ verdict: { level: "bad", headline: `read ${++n}`, reasons: [] } }) as unknown as Dashboard;
  let seen: Dashboard | null = null;
  function Probe({ open }: { open: boolean }) {
    seen = useOpenDashboard(open, load, 1e9);
    return null;
  }
  let r!: ReturnType<typeof create>;
  const flush = async () => {
    for (let i = 0; i < 6; i++) await Promise.resolve();
  };
  await act(async () => {
    r = create(<Probe open />);
  });
  await act(flush);
  expect(seen!.verdict.headline).toBe("read 1");
  await act(async () => r.update(<Probe open={false} />));
  expect(seen).toBeNull();
  await act(async () => r.update(<Probe open />));
  // the very first paint of the reopen carries no old verdict
  await act(flush);
  expect(seen!.verdict.headline).toBe("read 2");
  act(() => r.unmount());
});
