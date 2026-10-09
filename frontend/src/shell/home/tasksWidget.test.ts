import { expect, test } from "bun:test";
import { homeTasks } from "./data";
import { openCount, taskLane, taskPill } from "./widgets/TasksWidget";

const t = (o: Record<string, unknown>) => ({ key: "k", task_id: "T-1", status: "done", last_active: 1, ...o }) as any;

test("homeTasks: drops archived, keeps done and drafts, newest first", () => {
  const out = homeTasks([
    t({ key: "a", last_active: 5 }),
    t({ key: "b", status: "archived", last_active: 9 }),
    t({ key: "c", status: "upcoming", kind: "draft", last_active: 7 }),
    t({ key: "d", last_active: 0 }),
    t({ key: "e", last_active: undefined }),
    t({ key: "f", status: "in_progress", last_active: 8 }),
  ]);
  expect(out.map((x) => x.key)).toEqual(["f", "c", "a", "d", "e"]);
});

test("taskLane mirrors the Tasks board lanes", () => {
  expect(taskLane(t({ status: "in_progress" }))).toBe("in_progress");
  expect(taskLane(t({ status: "queued" }))).toBe("in_progress");
  expect(taskLane(t({ status: "needs_attention" }))).toBe("blocked");
  expect(taskLane(t({ status: "blocked" }))).toBe("blocked");
  expect(taskLane(t({ status: "upcoming" }))).toBe("upcoming");
  expect(taskLane(t({ status: "upcoming", kind: "draft" }))).toBe("upcoming");
  expect(taskLane(t({ status: "done" }))).toBe("done");
});

test("openCount excludes done and drafts", () => {
  expect(
    openCount([t({}), t({ status: "in_progress" }), t({ status: "blocked" }), t({ status: "upcoming", kind: "draft" })]),
  ).toBe(2);
});

test("taskPill", () => {
  expect(taskPill(t({ status: "in_progress" }))).toEqual({ label: "Running", tone: "ok" });
  expect(taskPill(t({ status: "queued" }))).toEqual({ label: "Queued", tone: "idle" });
  expect(taskPill(t({ status: "needs_attention" }))).toEqual({ label: "Needs you", tone: "warn" });
  expect(taskPill(t({ status: "upcoming", kind: "draft" }))).toEqual({ label: "Draft", tone: "idle" });
  expect(taskPill(t({ status: "upcoming" }))).toEqual({ label: "Upcoming", tone: "idle" });
  expect(taskPill(t({}))).toEqual({ label: "Done", tone: "idle" });
  expect(taskPill(t({ failed: true }))).toEqual({ label: "Failed", tone: "err" });
});
