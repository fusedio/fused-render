// The Workspace view's decisions, pure: which rows, in what order, what holds
// each one open, and what a key press means. No DOM.
import { describe, expect, test } from "bun:test";
import type { Task } from "@platform/lib/api";
import {
  holdReasons,
  holdMarker,
  cardStatusWord,
  visibleReasons,
  moveSelection,
  nextNeedingYou,
  searchTasks,
  statusWord,
  workspaceKeyAction,
  workspaceTasks,
  workspaceGroup,
  groupTasks,
  type WorkspaceFacts,
} from "@shell/workspace-lib";

function task(over: Partial<Task> = {}): Task {
  return {
    key: "s1",
    task_id: "T001",
    title: "t",
    status: "done",
    unread: 0,
    started: 100,
    session_id: "s1",
    project: "/p",
    target: "/p",
    ...over,
  } as Task;
}
const facts = (over: Partial<WorkspaceFacts> = {}): WorkspaceFacts => ({
  holds: {},
  holdsReady: true,
  prStates: {},
  ...over,
});

describe("workspaceTasks", () => {
  test("every non-archived, non-draft task, newest creation first", () => {
    const rows = workspaceTasks([
      task({ key: "a", started: 10 }),
      task({ key: "b", started: 30 }),
      task({ key: "c", started: 20, status: "archived" }),
      task({ key: "d", started: 40, kind: "draft" }),
    ]);
    expect(rows.map((t) => t.key)).toEqual(["b", "a"]);
  });
  test("a status change never moves a row", () => {
    const a = workspaceTasks([task({ key: "a", started: 1, status: "done" }), task({ key: "b", started: 2 })]);
    const b = workspaceTasks([task({ key: "a", started: 1, status: "in_progress" }), task({ key: "b", started: 2 })]);
    expect(a.map((t) => t.key)).toEqual(b.map((t) => t.key));
  });
  test("equal creation times break by key, so the order is stable", () => {
    const rows = workspaceTasks([task({ key: "b", started: 5 }), task({ key: "a", started: 5 })]);
    expect(rows.map((t) => t.key)).toEqual(["a", "b"]);
  });
});

describe("statusWord", () => {
  test("today's words", () => {
    expect(statusWord(task({ status: "in_progress" }))).toBe("Running");
    expect(statusWord(task({ status: "needs_attention" }))).toBe("Needs attention");
    expect(statusWord(task({ status: "done" }))).toBe("Done");
    expect(statusWord(task({ status: "blocked", blocked_reason: "failed" }))).toBe("Failed");
    expect(statusWord(task({ status: "blocked", blocked_reason: "usage_limit" }))).toContain("Usage limit");
    expect(statusWord(task({ status: "blocked" }))).toBe("Blocked");
  });
});

describe("holdReasons / holdMarker", () => {
  test("a read, finished task that never touched git is clear", () => {
    expect(holdReasons(task(), facts())).toEqual([]);
    expect(holdMarker([])).toBe("clear");
  });
  test("running and waiting on you hold it open", () => {
    expect(holdReasons(task({ status: "in_progress" }), facts())).toEqual(["running"]);
    expect(holdReasons(task({ status: "needs_attention" }), facts())).toEqual(["needs you"]);
    expect(holdReasons(task({ status: "blocked" }), facts())).toEqual(["needs you"]);
  });
  test("unread, background jobs and a pending run", () => {
    const t = task({
      unread: 3,
      background_jobs: { count: 2, run_id: "r", jobs: [] },
      next_run: 999,
    });
    expect(holdReasons(t, facts())).toEqual(["3 unread", "2 bg jobs", "run pending"]);
  });
  test("an open or draft PR holds it; merged and closed do not", () => {
    const t = task({ prs: [{ repo: "a/b", number: 7, url: "u" }] });
    expect(holdReasons(t, facts({ prStates: { "a/b#7": "open" } }))).toEqual(["PR #7 open"]);
    expect(holdReasons(t, facts({ prStates: { "a/b#7": "draft" } }))).toEqual(["PR #7 draft"]);
    expect(holdReasons(t, facts({ prStates: { "a/b#7": "merged" } }))).toEqual([]);
    expect(holdReasons(t, facts({ prStates: { "a/b#7": "closed" } }))).toEqual([]);
  });
  test("a PR whose state is not known yet is not called clear", () => {
    const t = task({ prs: [{ repo: "a/b", number: 7, url: "u" }] });
    expect(holdReasons(t, facts())).toEqual(["PR #7 unknown"]);
    expect(holdReasons(t, facts({ prStates: { "a/b#7": null } }))).toEqual(["PR #7 unknown"]);
  });
  test("worktree git state: uncommitted, unpushed, unknown", () => {
    const holds = {
      s1: [
        { path: "/w", dirty_files: 3, unpushed: 2, reasons: ["dirty", "unpushed"] },
        { path: "/x", dirty_files: null, unpushed: null, reasons: ["unknown"] },
      ],
    };
    expect(holdReasons(task(), facts({ holds }))).toEqual(["3 uncommitted", "2 unpushed", "unknown"]);
  });
  test("two worktrees add their counts", () => {
    const holds = {
      s1: [
        { path: "/w", dirty_files: 1, unpushed: 0, reasons: ["dirty"] },
        { path: "/x", dirty_files: 2, unpushed: 0, reasons: ["dirty"] },
      ],
    };
    expect(holdReasons(task(), facts({ holds }))).toEqual(["3 uncommitted"]);
  });
  test("before the holds have loaded, a task is never reported clear", () => {
    expect(holdReasons(task(), facts({ holdsReady: false }))).toEqual(["checking"]);
  });
  test("a failed holds read is unknown, never clear", () => {
    expect(holdReasons(task(), facts({ holdsReady: false, holdsFailed: true }))).toEqual(["unknown"]);
  });
  test("reasons join with a middle dot", () => {
    expect(holdMarker(["running", "2 unpushed"])).toBe("running · 2 unpushed");
  });
});

describe("selection", () => {
  const rows = [task({ key: "a" }), task({ key: "b" }), task({ key: "c" })];
  test("j and k step and clamp", () => {
    expect(moveSelection(rows, "a", 1)).toBe("b");
    expect(moveSelection(rows, "c", 1)).toBe("c");
    expect(moveSelection(rows, "a", -1)).toBe("a");
  });
  test("from no selection j takes the first and k the last", () => {
    expect(moveSelection(rows, null, 1)).toBe("a");
    expect(moveSelection(rows, null, -1)).toBe("c");
  });
  test("a selection that is gone starts over", () => {
    expect(moveSelection(rows, "zzz", 1)).toBe("a");
  });
  test("no rows, no selection", () => {
    expect(moveSelection([], null, 1)).toBeNull();
  });
});

describe("nextNeedingYou", () => {
  const rows = [
    task({ key: "a" }),
    task({ key: "b", status: "needs_attention" }),
    task({ key: "c" }),
    task({ key: "d", status: "blocked" }),
  ];
  test("the next row after the selection, wrapping", () => {
    expect(nextNeedingYou(rows, null)).toBe("b");
    expect(nextNeedingYou(rows, "b")).toBe("d");
    expect(nextNeedingYou(rows, "d")).toBe("b");
  });
  test("none needs you", () => {
    expect(nextNeedingYou([task()], null)).toBeNull();
  });
});

describe("searchTasks", () => {
  const all = [
    task({ key: "a", task_id: "T123", title: "Fix login", started: 1, status: "archived" }),
    task({ key: "b", task_id: "T124", title: "Login page", started: 2 }),
    task({ key: "c", task_id: "T200", title: "Other", started: 3 }),
    task({ key: "d", task_id: "", title: "login draft", started: 4, kind: "draft" }),
  ];
  test("title match lists open tasks before closed ones", () => {
    expect(searchTasks(all, "login").map((t) => t.key)).toEqual(["b", "a"]);
  });
  test("a T-number finds its task, with or without the T", () => {
    expect(searchTasks(all, "T123").map((t) => t.key)).toEqual(["a"]);
    expect(searchTasks(all, "123").map((t) => t.key)).toEqual(["a"]);
  });
  test("empty query lists everything, open first then closed, newest first", () => {
    expect(searchTasks(all, "").map((t) => t.key)).toEqual(["c", "b", "a"]);
  });
  test("drafts are never results", () => {
    expect(searchTasks(all, "draft")).toEqual([]);
  });
});

describe("workspaceKeyAction", () => {
  const base = { key: "", metaKey: false, ctrlKey: false, altKey: false, shiftKey: false };
  const ctx = { typing: false, paletteOpen: false };
  const press = (key: string, over: object = {}, c = ctx) =>
    workspaceKeyAction({ ...base, key, ...over }, c);
  test("the keymap", () => {
    expect(press("j")).toBe("next");
    expect(press("k")).toBe("prev");
    expect(press("Enter")).toBe("open");
    expect(press("x")).toBe("close");
    expect(press("c")).toBe("new");
    expect(press("/")).toBe("search");
    expect(press("n")).toBe("needs");
    expect(press("Escape")).toBe("escape");
    expect(press("k", { metaKey: true })).toBe("search");
  });
  test("typing in a field swallows the letters but not ⌘K", () => {
    const typing = { typing: true, paletteOpen: false };
    expect(press("j", {}, typing)).toBeNull();
    expect(press("x", {}, typing)).toBeNull();
    expect(press("k", { metaKey: true }, typing)).toBe("search");
  });
  test("while the palette is open the view's keys are not its own", () => {
    const open = { typing: false, paletteOpen: true };
    expect(press("j", {}, open)).toBeNull();
    expect(press("Escape", {}, open)).toBeNull();
  });
  test("other modifiers pass through", () => {
    expect(press("c", { metaKey: true })).toBeNull();
    expect(press("x", { ctrlKey: true })).toBeNull();
    expect(press("j", { altKey: true })).toBeNull();
  });
});

describe("cardStatusWord", () => {
  test("is spoken only for running and needs-you tasks", () => {
    expect(cardStatusWord(task({ status: "in_progress" }))).toBe("Running");
    expect(cardStatusWord(task({ status: "needs_attention" }))).toBe("Needs attention");
    expect(cardStatusWord(task({ status: "blocked", blocked_reason: "failed" }))).toBe("Failed");
    expect(cardStatusWord(task({ status: "blocked", blocked_reason: "usage_limit" }))).toContain("Usage limit");
    expect(cardStatusWord(task({ status: "done" }))).toBe("");
    expect(cardStatusWord(task({ status: "stopped" as Task["status"] }))).toBe("");
  });
});

describe("visibleReasons", () => {
  test("drops unread and chip-duplicate reasons, keeps worktree facts", () => {
    const all = ["3 unread", "PR #7 open", "PR #8 draft", "2 bg jobs", "1 uncommitted", "2 unpushed", "unknown"];
    expect(visibleReasons(all, task())).toEqual(["1 uncommitted", "2 unpushed", "unknown"]);
  });
  test("keeps an unknown PR, queued and a pending run", () => {
    expect(visibleReasons(["PR #7 unknown", "queued", "run pending"], task())).toEqual([
      "PR #7 unknown",
      "queued",
      "run pending",
    ]);
  });
  test("drops running / needs you only while the status word is shown", () => {
    expect(visibleReasons(["running"], task({ status: "in_progress" }))).toEqual([]);
    expect(visibleReasons(["needs you"], task({ status: "needs_attention" }))).toEqual([]);
    expect(visibleReasons(["running"], task({ status: "done" }))).toEqual(["running"]);
  });
});

import { bgJobChips, capList, messagePreview } from "./workspace-lib";

test("capList keeps the first max and counts the rest", () => {
  expect(capList([1, 2, 3, 4], 3)).toEqual({ shown: [1, 2, 3], more: 1 });
  expect(capList([1], 3)).toEqual({ shown: [1], more: 0 });
});

test("bgJobChips names up to three jobs and counts the rest", () => {
  const t = (count: number, titles: string[]) =>
    ({ background_jobs: { count, run_id: "r", jobs: titles.map((x) => ({ id: x, title: x })) } }) as never;
  expect(bgJobChips(t(4, ["a", "b", "c", "d"]))).toEqual({ titles: ["a", "b", "c"], more: 1 });
  expect(bgJobChips(t(2, []))).toEqual({ titles: [], more: 2 });
});

test("messagePreview is empty when it equals the title", () => {
  expect(messagePreview({ title: "Hi", last_message: { role: "user", text: "Hi", at: 1 } } as never)).toBe("");
  expect(messagePreview({ title: "Hi", last_message: { role: "user", text: "Other", at: 1 } } as never)).toBe("Other");
});

import { pickBranch } from "./workspace-lib";

test("pickBranch: a worktree branch beats the PR head, then the newest PR head", () => {
  expect(pickBranch([{ branch: "wt-a" }, { branch: "wt-b" }, { branch: "wt-a" }], ["pr-x"])).toEqual({ name: "wt-a", more: 1 });
  expect(pickBranch([{ branch: null }], [undefined, "pr-x", "pr-y"])).toEqual({ name: "pr-x", more: 0 });
  expect(pickBranch([], [])).toBeNull();
});

describe("workspaceGroup / groupTasks", () => {
  const pr = { prs: [{ repo: "o/r", number: 1 }] } as Partial<Task>;
  const open = facts({ prStates: { "o/r#1": "open" } });
  test("one case per group", () => {
    expect(workspaceGroup(task({ status: "needs_attention" }), facts())).toBe("needs");
    expect(workspaceGroup(task({ status: "blocked", blocked_reason: "failed" }), facts())).toBe("needs");
    expect(workspaceGroup(task({ status: "blocked", blocked_reason: "usage_limit" }), facts())).toBe("needs");
    expect(workspaceGroup(task({ status: "in_progress" }), facts())).toBe("running");
    expect(workspaceGroup(task({ ...pr }), open)).toBe("review");
    expect(workspaceGroup(task({ unread: 2 }), facts())).toBe("idle");
    expect(workspaceGroup(task(), facts())).toBe("clear");
  });
  test("priority: running beats review, needs beats holds", () => {
    expect(workspaceGroup(task({ status: "in_progress", ...pr }), open)).toBe("running");
    expect(workspaceGroup(task({ status: "needs_attention", unread: 3 }), facts())).toBe("needs");
  });
  test("groups come in order, empty ones omitted, rows keep their order", () => {
    const rows = [
      task({ key: "a" }),
      task({ key: "b", status: "needs_attention" }),
      task({ key: "c" }),
      task({ key: "d", status: "needs_attention" }),
    ];
    const g = groupTasks(rows, () => facts());
    expect(g.map((x) => [x.key, x.label, x.tasks.map((t) => t.key)])).toEqual([
      ["needs", "Needs you", ["b", "d"]],
      ["clear", "Safe to archive", ["a", "c"]],
    ]);
  });
});
