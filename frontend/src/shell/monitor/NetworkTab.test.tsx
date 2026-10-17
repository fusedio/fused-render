// The Network tab rendered against fixed payloads (the api injected, the poll
// parked on a huge interval).
import { afterEach, expect, test } from "bun:test";
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

test("summary strip, six slots with the overflow queued, and the groups busiest first", async () => {
  const { r } = await mount();
  const strip = text(byClass(r, "monitor-net-strip")[0]);
  expect(strip).toContain("4.5");
  expect(strip).toContain("640 ms");
  expect(strip).toContain("Open WebSockets2");
  expect(byClass(r, "monitor-slot")).toHaveLength(6);
  expect(byClass(r, "is-held")).toHaveLength(6);
  expect(text(byClass(r, "monitor-slots-queued")[0])).toBe("+2 queued");
  const groups = byClass(r, "monitor-group-row").map((g) => g.props["data-group"]);
  expect(groups).toEqual(["page-a /api/x", "page-b /api/y"]);
});

test("open requests show a live timer from their start time", async () => {
  const { r } = await mount();
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
