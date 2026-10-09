import { expect, test } from "bun:test";
import { homeTasks, splitHomeTasks } from "./data";
import { emptyMessage, lanesFor, listFor, laneOf, listOrder, openCount, taskPill } from "./widgets/TasksWidget";

const t = (o: Record<string, unknown>) => ({ key: "k", task_id: "T-1", status: "done", last_active: 1, ...o }) as any;

test("homeTasks: drops archived and drafts, keeps done, newest first", () => {
  const out = homeTasks([
    t({ key: "a", last_active: 5 }),
    t({ key: "b", status: "archived", last_active: 9 }),
    t({ key: "c", status: "upcoming", kind: "draft", last_active: 7 }),
    t({ key: "d", last_active: 0 }),
    t({ key: "e", last_active: undefined }),
    t({ key: "f", status: "in_progress", last_active: 8 }),
  ]);
  expect(out.map((x) => x.key)).toEqual(["f", "a", "d", "e"]);
});

test("laneOf maps statuses to the four lanes", () => {
  expect(laneOf(t({ status: "in_progress" }))).toBe("running");
  expect(laneOf(t({ status: "queued" }))).toBe("queued");
  expect(laneOf(t({ status: "upcoming" }))).toBe("queued");
  expect(laneOf(t({ status: "needs_attention" }))).toBe("you");
  expect(laneOf(t({ status: "blocked" }))).toBe("you");
  expect(laneOf(t({ status: "done" }))).toBe("done");
});

test("listOrder: open tasks before done, order kept within each group", () => {
  const out = listOrder([
    t({ key: "d1" }),
    t({ key: "o1", status: "in_progress" }),
    t({ key: "d2" }),
    t({ key: "o2", status: "queued" }),
  ]);
  expect(out.map((x) => x.key)).toEqual(["o1", "o2", "d1", "d2"]);
});

test("openCount excludes done", () => {
  expect(openCount([t({}), t({ status: "in_progress" }), t({ status: "blocked" })])).toBe(2);
  expect(openCount([t({}), t({})])).toBe(0);
});

test("taskPill", () => {
  expect(taskPill(t({ status: "in_progress" }))).toEqual({ label: "Running", tone: "ok" });
  expect(taskPill(t({ status: "queued" }))).toEqual({ label: "Queued", tone: "idle" });
  expect(taskPill(t({ status: "needs_attention" }))).toEqual({ label: "Needs you", tone: "warn" });
  expect(taskPill(t({}))).toEqual({ label: "Done", tone: "idle" });
  expect(taskPill(t({ failed: true }))).toEqual({ label: "Failed", tone: "err" });
});

test("splitHomeTasks counts what it filtered out", () => {
  const r = splitHomeTasks([
    t({ key: "a" }),
    t({ key: "b", status: "archived" }),
    t({ key: "c", status: "archived", kind: "draft" }),
    t({ key: "d", status: "upcoming", kind: "draft" }),
  ]);
  expect(r.tasks.map((x) => x.key)).toEqual(["a"]);
  expect(r.drafts).toBe(1);
  expect(r.archived).toBe(2);
});

test("emptyMessage", () => {
  expect(emptyMessage({ drafts: 0, archived: 0 })).toBe("No tasks yet.");
  expect(emptyMessage({ drafts: 1, archived: 0 })).toBe("No active tasks · 1 draft");
  expect(emptyMessage({ drafts: 3, archived: 0 })).toBe("No active tasks · 3 drafts");
  expect(emptyMessage({ drafts: 0, archived: 19 })).toBe("No active tasks · 19 archived");
  expect(emptyMessage({ drafts: 1, archived: 19 })).toBe("No active tasks · 1 draft, 19 archived");
});

test("lanesFor: Done lane only when done tasks show", () => {
  expect(lanesFor("open_done").map((l) => l.id)).toEqual(["queued", "running", "you", "done"]);
  expect(lanesFor("open").map((l) => l.id)).toEqual(["queued", "running", "you"]);
});

test("listFor: open only drops done; open_done lists open first", () => {
  const ts = [t({ key: "d" }), t({ key: "o", status: "queued" })];
  expect(listFor(ts, "open").map((x) => x.key)).toEqual(["o"]);
  expect(listFor(ts, "open_done").map((x) => x.key)).toEqual(["o", "d"]);
  expect(listFor([t({})], "open")).toEqual([]);
});
