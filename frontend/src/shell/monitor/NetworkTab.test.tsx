// The Network tab rendered against fixed payloads (the api injected, the poll
// parked on a huge interval).
import { afterEach, expect, jest, test } from "bun:test";
import { act, create, type ReactTestInstance, type ReactTestRenderer } from "react-test-renderer";

import type { NetFilters, NetGroup, NetRow, NetSummary } from "@shell/monitor/monitor-api";
import NetworkTab, { type NetworkApi } from "@shell/monitor/NetworkTab";

function row(id: number, over: Partial<NetRow> = {}): NetRow {
  return {
    id, seq: id, t: 1000 + id, method: "GET", route: "/api/x", path: "/api/x?a=",
    status: 200, server_ms: 12, held_ms: 12, bytes: 2048, source: "page-a", client: "c1",
    outcome: "ok", self: false, ...over,
  };
}
const OPEN = (id: number) => row(id, { status: null, outcome: null, server_ms: null, held_ms: null });

const SUMMARY: NetSummary = {
  rps_10s: 4.5, rps_60s: 3, in_flight: [{ client: "c1", in_flight: 8, saturated_s: 3, top: { source: "page-a", route: "/api/x", count: 8 } }],
  in_flight_total: 8, oldest_in_flight_s: 3, errors_60s: 2, long_held: [], browser_queue_p95_ms: 640,
  concurrency: [{ client: "c1", peak: 7, series: [...Array(58).fill(1), 7, 2], at_limit_s: 3.5 }],
  main_client: "c1",
};
const GROUPS: NetGroup[] = [
  { source: "page-a", route: "/api/x", count: 90, per_min: 90, p50_ms: 10, p95_ms: 40, errors: 1, max_in_flight: 8, queue_p95_ms: 600, queue_max_ms: 900 },
  { source: "page-b", route: "/api/y", count: 3, per_min: 3, p50_ms: 5, p95_ms: 6, errors: 0, max_in_flight: 1, queue_p95_ms: null, queue_max_ms: null },
];

let renderer: ReactTestRenderer | null = null;
afterEach(() => {
  act(() => renderer?.unmount());
  renderer = null;
});

const text = (n: ReactTestInstance | string): string =>
  typeof n === "string" ? n : n.children.map((c) => text(c as ReactTestInstance | string)).join("");
const byClass = (r: ReactTestRenderer, c: string) =>
  r.root.findAll((n) => typeof n.type === "string" && n.props.className?.split(" ").includes(c));

async function mount(
  over: Partial<NetworkApi> = {},
  props: Record<string, unknown> = {},
): Promise<{ r: ReactTestRenderer; calls: { since: number; f: NetFilters }[] }> {
  const calls: { since: number; f: NetFilters }[] = [];
  const open = Array.from({ length: 8 }, (_, i) => OPEN(i + 1));
  const api: NetworkApi = {
    summary: async () => SUMMARY,
    groups: async () => ({ groups: GROUPS }),
    sockets: async () => ({ sockets: [{ kind: "run", source: "page-a", open: 2, msgs_per_s: 1.5 }] }),
    requests: async (since, f) => {
      calls.push({ since, f });
      if (f.openOnly && !f.source) return { rows: open, cursor: 8 };
      return { rows: [row(20), OPEN(21)], cursor: 21 };
    },
    ...over,
  };
  await act(async () => {
    renderer = create(<NetworkTab api={api} pollMs={1e9} now={() => 1030} {...props} />);
  });
  await act(async () => {
    for (let i = 0; i < 8; i++) await Promise.resolve();
  });
  return { r: renderer!, calls };
}

test("summary strip, and the groups busiest first", async () => {
  const { r } = await mount();
  const strip = text(byClass(r, "monitor-net-strip")[0]);
  expect(strip).toContain("4.5");
  expect(strip).toContain("640 ms");
  expect(strip).toContain("Browser wait p95");
  expect(strip).not.toContain("In flight");
  expect(strip).not.toContain("Open WebSockets");
  expect(strip).toContain("Errors/min2");
  const groups = byClass(r, "monitor-group-row").map((g) => g.props["data-group"]);
  expect(groups).toEqual(["page-a /api/x", "page-b /api/y"]);
});

test("open requests show a live timer from their start time", async () => {
  const { r } = await mount({}, { initialRoute: "/api/x" });
  const open = byClass(r, "monitor-req-row").find((n) => n.props["data-req"] === 21)!;
  expect(text(open)).toContain("open");
  expect(text(open)).toContain("9.0 s"); // now 1030 - t 1021
});

test("clicking a group filters the list and starts again from cursor 0", async () => {
  const { r, calls } = await mount();
  const g = byClass(r, "monitor-group-row")[0];
  await act(async () => g.props.onClick());
  await act(async () => {
    for (let i = 0; i < 8; i++) await Promise.resolve();
  });
  const last = calls[calls.length - 1];
  expect(last.f.source).toBe("page-a");
  expect(last.f.route).toBe("/api/x");
  expect(last.since).toBe(0);
});

test("?source=&route= seed the filters, and Pause stops fetching the list", async () => {
  const { r, calls } = await mount({}, { initialSource: "page-b", initialRoute: "/api/y" });
  expect(calls.some((c) => c.f.source === "page-b" && c.f.route === "/api/y")).toBe(true);
  const pause = r.root.find((n) => n.type === "button" && text(n) === "Pause");
  act(() => pause.props.onClick());
  expect(text(r.root.find((n) => n.type === "button" && n.props["aria-pressed"] === true))).toBe("Resume");
});

test("text filters debounce 300 ms before the poll sees them; toggles are immediate; unmount clears the timer", async () => {
  jest.useFakeTimers();
  try {
    const { r, calls } = await mount({}, { initialRoute: "/api/x" });
    const input = r.root.findByProps({ "aria-label": "Filter by page" });
    const fire = async () => {
      await act(async () => {
        jest.advanceTimersByTime(0);
        for (let i = 0; i < 8; i++) await Promise.resolve();
      });
    };
    const before = calls.length;
    await act(async () => input.props.onChange({ target: { value: "p" } }));
    await act(async () => input.props.onChange({ target: { value: "pa" } }));
    // the box shows the draft at once; no request carries it yet
    expect(r.root.findByProps({ "aria-label": "Filter by page" }).props.value).toBe("pa");
    await act(async () => {
      jest.advanceTimersByTime(100);
    });
    await fire();
    expect(calls.slice(before).some((c) => c.f.source === "pa")).toBe(false);
    await act(async () => {
      jest.advanceTimersByTime(200);
    });
    await fire();
    expect(calls.slice(before).some((c) => c.f.source === "pa")).toBe(true);

    // a toggle commits immediately
    const open = r.root.findAll((n) => n.type === "button" && text(n) === "open only")[0];
    const mark = calls.length;
    await act(async () => open.props.onClick());
    await fire();
    expect(calls.slice(mark).some((c) => c.f.openOnly)).toBe(true);

    // a pending debounce is cleared on unmount (nothing left to fire)
    await act(async () => r.root.findByProps({ "aria-label": "Filter by route" }).props.onChange({ target: { value: "/x" } }));
    expect(jest.getTimerCount()).toBeGreaterThan(0);
    act(() => renderer?.unmount());
    renderer = null;
    expect(jest.getTimerCount()).toBe(0);
  } finally {
    jest.useRealTimers();
  }
});

const many = (n: number): NetGroup[] =>
  Array.from({ length: n }, (_, i) => ({
    ...GROUPS[0], source: `page-${i}`, route: `/api/r${i}`, per_min: 100 - i, errors: i === 0 ? 3 : 0,
  }));
const btn = (r: ReactTestRenderer, label: string) =>
  r.root.findAll((n) => n.type === "button" && text(n) === label)[0];

test("errors tile is hidden at 0 errors", async () => {
  const { r } = await mount({ summary: async () => ({ ...SUMMARY, errors_60s: 0 }) });
  expect(text(byClass(r, "monitor-net-strip")[0])).not.toContain("Errors/min");
});

test("groups show the top 5 with a Show all toggle, an err badge, and no monitor-own rows", async () => {
  const groups = [...many(7), { ...GROUPS[0], source: "monitor", route: "/api/system/net/groups", per_min: 1000 }];
  const { r } = await mount({ groups: async () => ({ groups }) });
  expect(byClass(r, "monitor-group-row")).toHaveLength(5);
  expect(text(byClass(r, "monitor-net-err")[0])).toBe("3 err");
  expect(text(byClass(r, "monitor-net-table")[0])).not.toContain("Max in flight");
  await act(async () => btn(r, "Show all (7)").props.onClick());
  expect(byClass(r, "monitor-group-row")).toHaveLength(7);
  await act(async () => btn(r, "Show fewer").props.onClick());
  expect(byClass(r, "monitor-group-row")).toHaveLength(5);
});

test("request log is collapsed by default and does not poll the filtered list", async () => {
  const { r, calls } = await mount();
  expect(btn(r, "Show request log")).toBeDefined();
  expect(r.root.findAllByProps({ "aria-label": "Filter by page" })).toHaveLength(0);
  expect(byClass(r, "monitor-req-row")).toHaveLength(0);
  expect(calls.every((c) => c.f.openOnly)).toBe(true);
  await act(async () => btn(r, "Show request log").props.onClick());
  await act(async () => {
    for (let i = 0; i < 8; i++) await Promise.resolve();
  });
  expect(btn(r, "Hide request log")).toBeDefined();
  expect(calls.some((c) => !c.f.openOnly)).toBe(true);
  expect(byClass(r, "monitor-req-row").length).toBeGreaterThan(0);
});

test("deep link expands the log; non-GET methods prefix the route", async () => {
  const { r } = await mount(
    { requests: async (_s, f) => (f.openOnly && !f.source ? { rows: [], cursor: 0 } : { rows: [row(5, { method: "POST", route: "/api/run" })], cursor: 5 }) },
    { initialSource: "page-a" },
  );
  expect(btn(r, "Hide request log")).toBeDefined();
  expect(r.root.findByProps({ "aria-label": "Filter by page" }).props.value).toBe("page-a");
  expect(text(byClass(r, "monitor-req-row")[0])).toContain("POST /api/run");
});

test("no verdict banner; slow browser wait tints the stat", async () => {
  const { r } = await mount();
  expect(byClass(r, "monitor-verdict")).toHaveLength(0);
  expect(byClass(r, "monitor-net-stat").filter((n) => n.props.className.includes("is-warn"))).toHaveLength(1);
  const { r: r2 } = await mount({
    summary: async () => ({ ...SUMMARY, browser_queue_p95_ms: 20 }),
    requests: async () => ({ rows: [], cursor: 0 }),
  });
  expect(byClass(r2, "monitor-net-stat").filter((n) => n.props.className.includes("is-warn"))).toHaveLength(0);
});

test("Connections card shows peak, time at the limit and a chart; held requests list below", async () => {
  const { r } = await mount();
  const card = byClass(r, "monitor-net-conn")[0];
  expect(card.props.className).toContain("is-warn");
  expect(text(card)).toContain("Connections");
  expect(text(card)).toContain("peak 7 of 6");
  expect(text(card)).toContain("last 60 s · at the limit 3.5 s");
  expect(text(card)).toContain("6 limit");
  expect(card.findAll((n) => n.props.className === "monitor-chart")).toHaveLength(1);
  // OPEN rows were opened at t=1001..1008 and now() is 1030: all held > 2 s.
  const held = byClass(r, "monitor-net-held")[0];
  expect(held.findAll((n) => n.type === "li")).toHaveLength(8);
  expect(text(held)).toContain("GET /api/x · from page-a · 29 s");
});

test("Connections card is calm when never at the limit and nothing is held", async () => {
  const { r } = await mount({
    summary: async () => ({ ...SUMMARY, concurrency: [{ client: "c1", peak: 2, series: Array(60).fill(1), at_limit_s: 0 }] }),
    requests: async () => ({ rows: [], cursor: 0 }),
  });
  const card = byClass(r, "monitor-net-conn")[0];
  expect(card.props.className).not.toContain("is-warn");
  expect(text(card)).toContain("peak 2 of 6");
  expect(text(card)).toContain("never at the limit");
  expect(byClass(r, "monitor-net-held")).toHaveLength(0);
});
