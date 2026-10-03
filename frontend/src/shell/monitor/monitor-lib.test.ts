// The /monitor page's pure rules (monitor-lib.ts).
import { expect, test } from "bun:test";

import type { MachineProc } from "@platform/lib/sysmon";
import {
  clickSelect,
  cpuTone,
  EMPTY_SELECTION,
  endRoute,
  bulkVerb,
  confirmQuestion,
  fusedTotals,
  holdOrder,
  memOfTotal,
  nextSort,
  niceCeil,
  sortProcs,
  summarizeResults,
} from "@shell/monitor/monitor-lib";

function p(pid: number, over: Partial<MachineProc> = {}): MachineProc {
  return {
    pid,
    ppid: 1,
    user: "me",
    command: `c${pid}`,
    args: `c${pid}`,
    cpuPct: null,
    memBytes: null,
    startedAt: pid,
    fused: false,
    kind: "system",
    label: `c${pid}`,
    ...over,
  };
}

test("nulls sort last whichever way the column runs; pid breaks ties", () => {
  const procs = [p(1), p(2, { cpuPct: 5 }), p(3, { cpuPct: 50 }), p(4, { cpuPct: 5 })];
  expect(sortProcs(procs, { key: "cpu", dir: "desc" }).map((x) => x.pid)).toEqual([3, 2, 4, 1]);
  expect(sortProcs(procs, { key: "cpu", dir: "asc" }).map((x) => x.pid)).toEqual([2, 4, 3, 1]);
});

test("a header's first click starts numbers high and names at A; the second flips", () => {
  expect(nextSort({ key: "cpu", dir: "desc" }, "mem")).toEqual({ key: "mem", dir: "desc" });
  expect(nextSort({ key: "cpu", dir: "desc" }, "name")).toEqual({ key: "name", dir: "asc" });
  expect(nextSort({ key: "mem", dir: "desc" }, "mem")).toEqual({ key: "mem", dir: "asc" });
});

test("click selects one, toggle adds/removes, range runs from the anchor", () => {
  const order = [10, 20, 30, 40];
  let s = clickSelect(EMPTY_SELECTION, order, 20, { toggle: false, range: false });
  expect([...s.pids]).toEqual([20]);
  s = clickSelect(s, order, 40, { toggle: false, range: true });
  expect([...s.pids].sort()).toEqual([20, 30, 40]);
  s = clickSelect(s, order, 30, { toggle: true, range: false });
  expect([...s.pids].sort()).toEqual([20, 40]);
  s = clickSelect(s, order, 10, { toggle: false, range: false });
  expect([...s.pids]).toEqual([10]);
});

test("owned fused rows stop through their owner, the rest are signalled, the app never", () => {
  expect(endRoute(p(1, { fused: true, kind: "app" }))).toBeNull();
  expect(endRoute(p(2, { fused: true, kind: "model" }))).toBe("stop");
  expect(endRoute(p(3, { fused: true, kind: "other" }))).toBe("kill");
  expect(endRoute(p(4))).toBe("kill");
});

test("a batch reads as one line", () => {
  const r = (outcome: "ended" | "denied" | "gone" | "refused") => ({ pid: 1, name: "x", outcome });
  expect(summarizeResults([r("ended"), r("ended"), r("denied"), r("gone")])).toBe(
    "Killed 2 · 1 permission denied · 1 already gone",
  );
  expect(summarizeResults([r("ended")], true)).toBe("Force killed x");
  expect(summarizeResults([r("ended"), r("ended")], false, "Stop")).toBe("Stopped 2");
});

test("figures: CPU tone, fused totals, memory of total, nice ceilings", () => {
  expect([cpuTone(null), cpuTone(40), cpuTone(60), cpuTone(150)]).toEqual([
    undefined,
    undefined,
    "warm",
    "hot",
  ]);
  expect(
    fusedTotals([p(1, { fused: true, cpuPct: 2, memBytes: 10 }), p(2, { fused: true, memBytes: 5 }), p(3, { cpuPct: 99 })]),
  ).toEqual({ cpuPct: 2, memBytes: 15 });
  expect(memOfTotal(12.6 * 1024 ** 3, 16 * 1024 ** 3)).toBe("12.6 of 16 GB");
  expect([niceCeil(7), niceCeil(11), niceCeil(230), niceCeil(0)]).toEqual([10, 20, 250, 1]);
});

test("a held order keeps its rows in place, drops exited pids, appends newcomers", () => {
  expect(holdOrder([3, 1, 2], [1, 2, 3])).toEqual([3, 1, 2]);
  expect(holdOrder([3, 1, 2], [9, 2, 3])).toEqual([3, 2, 9]);
  const once = holdOrder([3, 1, 2], [9, 2, 3, 1]);
  expect(holdOrder(once, [9, 2, 3, 1])).toEqual(once); // idempotent
});

test("the bar says Stop only when every selected row is owner-stoppable", () => {
  expect(bulkVerb([p(1, { fused: true, kind: "claude" }), p(2, { fused: true, kind: "model" })])).toBe("Stop");
  expect(bulkVerb([p(1, { fused: true, kind: "claude" }), p(2)])).toBe("Kill");
  expect(bulkVerb([])).toBe("Kill");
});

test("the confirm names up to three processes, then counts and warns", () => {
  const ps = Array.from({ length: 12 }, (_, i) => p(100 + i, { command: `app${i}` }));
  expect(confirmQuestion(ps.slice(0, 1), "Kill")).toBe("Kill app0 (100)?");
  expect(confirmQuestion(ps.slice(0, 2), "Kill")).toBe("Kill app0 (100) and app1 (101)?");
  expect(confirmQuestion(ps.slice(0, 3), "Stop")).toBe("Stop app0 (100), app1 (101) and app2 (102)?");
  expect(confirmQuestion(ps.slice(0, 5), "Kill")).toBe(
    "Kill 5 processes? This includes app0 (100), app1 (101)…",
  );
  expect(confirmQuestion(ps, "Force kill")).toBe(
    "Force kill 12 processes? This includes app0 (100), app1 (101)… This may close apps you are using.",
  );
});
