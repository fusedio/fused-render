import { afterEach, expect, test } from "bun:test";
import { createElement } from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { formatBytes, formatCpu, resetSystemActivityForTests, useSystemActivity } from "@shell/system-lib";
import { setEventsClientForTests } from "@platform/lib/events";
import type { SystemActivity } from "@platform/lib/sysmon";

const MB = 1024 ** 2;

function payload(cpus: (number | null)[]): SystemActivity {
  return {
    supported: true,
    host: null,
    history: [],
    totals: { cpuPct: null, memBytes: null },
    procs: cpus.map((cpuPct, i) => ({
      pid: i + 1,
      ppid: null,
      kind: "other",
      label: `p${i}`,
      cpuPct,
      memBytes: 1,
      startedAt: null,
    })),
  };
}

test("formatBytes writes sizes the way Activity Monitor does", () => {
  expect(formatBytes(1.02 * 1024 * MB)).toBe("1.02 GB");
  expect(formatBytes(194.3 * MB)).toBe("194.3 MB");
  expect(formatBytes(812 * 1024)).toBe("812 KB");
  expect(formatBytes(1.44 * 1024 * MB, true)).toBe("1.4 GB");
  expect(formatBytes(null)).toBe("–");
});

test("formatCpu keeps one decimal and dashes an unknown", () => {
  expect(formatCpu(12.34)).toBe("12.3%");
  expect(formatCpu(0)).toBe("0.0%");
  expect(formatCpu(null)).toBe("–");
});

// The hub: one subscription to `system.activity` for fused-render's own
// processes however many components read it, fed by the fake events client
// (the real one has no socket in bun and never calls back).
let renderers: ReactTestRenderer[] = [];

afterEach(() => {
  act(() => renderers.forEach((r) => r.unmount()));
  renderers = [];
  setEventsClientForTests(null);
  resetSystemActivityForTests();
});

function Reader({ fast, seen }: { fast: boolean; seen: (d: SystemActivity | null) => void }) {
  seen(useSystemActivity(fast));
  return null;
}

function mount(fast: boolean, seen: (d: SystemActivity | null) => void): ReactTestRenderer {
  let r!: ReactTestRenderer;
  act(() => {
    r = create(createElement(Reader, { fast, seen }));
  });
  renderers.push(r);
  return r;
}

test("readers share ONE subscription, scoped to fused-render, closed with the last reader", () => {
  const calls: { topic: string; params: unknown }[] = [];
  let unsubscribes = 0;
  let push: ((snap: unknown) => void) | null = null;
  setEventsClientForTests({
    subscribe: ((topic: string, params: unknown, cb: (s: unknown, d: unknown, m: Record<string, unknown>) => void) => {
      calls.push({ topic, params });
      push = (snap) => cb(snap, null, { gen: null });
      return () => {
        unsubscribes += 1;
      };
    }) as never,
  });
  const seen: (SystemActivity | null)[] = [];
  mount(false, (d) => seen.push(d));
  // `fast` asks for nothing extra: the server pushes every sampler tick.
  mount(true, (d) => seen.push(d));
  expect(calls).toEqual([{ topic: "system.activity", params: { scope: "fused" } }]);
  expect(seen.every((d) => d === null)).toBe(true);

  act(() => push?.(payload([1, 50])));
  expect(seen.slice(-2).every((d) => d?.procs.length === 2)).toBe(true);

  act(() => renderers[0].unmount());
  expect(unsubscribes).toBe(0);
  act(() => renderers[1].unmount());
  renderers = [];
  expect(unsubscribes).toBe(1);
});

test("a refused frame keeps the last snapshot", () => {
  let cb: ((s: unknown, d: unknown, m: Record<string, unknown>) => void) | null = null;
  setEventsClientForTests({
    subscribe: ((_t: string, _p: unknown, c: typeof cb) => {
      cb = c;
      return () => {};
    }) as never,
  });
  const seen = { latest: null as SystemActivity | null };
  mount(false, (d) => {
    seen.latest = d;
  });
  act(() => cb?.(payload([3]), null, { gen: null }));
  expect(seen.latest?.procs.length).toBe(1);
  act(() => cb?.(null, null, { error: "sampler gone", status: 500 }));
  expect(seen.latest?.procs.length).toBe(1);
});
