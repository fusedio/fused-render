import { expect, test } from "bun:test";
import {
  archivePlan,
  confirmText,
  groupState,
  pruned,
  runPool,
  selectableTask,
  toggled,
  withMany,
} from "./task-selection";

test("toggled flips one key and never edits the old set", () => {
  const a = new Set<string>();
  const b = toggled(a, "x");
  expect([...b]).toEqual(["x"]);
  expect(a.size).toBe(0);
  expect(toggled(b, "x").size).toBe(0);
});

test("withMany adds or removes every key, and returns the same set for a no-op", () => {
  const a = withMany(new Set(), ["a", "b", "c"], true);
  expect([...a].sort()).toEqual(["a", "b", "c"]);
  expect(withMany(a, ["a", "b"], true)).toBe(a);
  const b = withMany(a, ["a", "z"], false);
  expect([...b].sort()).toEqual(["b", "c"]);
  expect(withMany(b, ["z"], false)).toBe(b);
});

test("groupState is all, some or none; an empty group is none", () => {
  const sel = new Set(["a", "b"]);
  expect(groupState(sel, ["a", "b"])).toBe("all");
  expect(groupState(sel, ["a", "c"])).toBe("some");
  expect(groupState(sel, ["c", "d"])).toBe("none");
  expect(groupState(sel, [])).toBe("none");
});

test("pruned drops keys that left the visible list, keeping the set when none did", () => {
  const sel = new Set(["a", "b", "c"]);
  expect(pruned(sel, ["a", "b", "c", "d"])).toBe(sel);
  expect([...pruned(sel, ["b"])]).toEqual(["b"]);
  expect(pruned(new Set(), ["a"]).size).toBe(0);
});

test("archivePlan counts running tasks among the selected; confirmText says so", () => {
  const tasks = [
    { key: "a", status: "in_progress" },
    { key: "b", status: "done" },
    { key: "c", status: "in_progress" },
  ];
  expect(archivePlan(tasks, new Set(["a", "b"]))).toEqual({ keys: ["a", "b"], running: 1 });
  expect(confirmText(3, 2)).toBe("Archive 3 tasks? 2 are still running and will be stopped.");
  expect(confirmText(1, 1)).toBe("Archive 1 task? 1 is still running and will be stopped.");
});

test("selectableTask leaves out archived and draft tasks", () => {
  expect(selectableTask({ status: "done" } as never)).toBe(true);
  expect(selectableTask({ status: "archived" } as never)).toBe(false);
});

test("runPool never has more than `limit` in flight and reports each outcome", async () => {
  let live = 0;
  let peak = 0;
  const out = await runPool([1, 2, 3, 4, 5, 6, 7, 8, 9], 4, async (n) => {
    live += 1;
    peak = Math.max(peak, live);
    await new Promise((r) => setTimeout(r, 2));
    live -= 1;
    if (n === 5) throw new Error("no");
  });
  expect(peak).toBe(4);
  expect(out.map((o) => o.ok)).toEqual([true, true, true, true, false, true, true, true, true]);
  expect(out[4]!.error?.message).toBe("no");
});
