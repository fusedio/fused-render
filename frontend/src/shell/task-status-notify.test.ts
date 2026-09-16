// The decision table behind §5's "interactive turns and needs-input" moments
// — pure, no DOM (task-status-notify.ts's own header explains the split).
import { describe, expect, test } from "bun:test";
import { notificationForTransition, taskDestination } from "./task-status-notify";
import type { TaskPulseTask } from "@platform/lib/api";

function task(over: Partial<TaskPulseTask> = {}): TaskPulseTask {
  return {
    key: "t1",
    status: "in_progress",
    unread: 0,
    last_active: 0,
    happened_at: 0,
    project: "/proj",
    task_id: "T097",
    title: "update the changelog",
    target: "/proj",
    session_id: "",
    ...over,
  } as TaskPulseTask;
}

describe("notificationForTransition", () => {
  test("a task's first sighting is never a transition", () => {
    expect(notificationForTransition(undefined, task({ status: "needs_attention" }))).toBeNull();
    expect(notificationForTransition(undefined, task({ status: "blocked" }))).toBeNull();
  });

  test("no status change is not a transition", () => {
    expect(notificationForTransition("in_progress", task({ status: "in_progress" }))).toBeNull();
  });

  // REVERSED 2026-09-16 (user: "the user does want to open the app along
  // with claude template to go back") — a finished task is now retained AND
  // clickable, not a plain "it's over" popup that vanishes. `isRetained`
  // (notifications.ts) keys retention on `Boolean(action || page)`, so `page`
  // being set is what actually keeps it — this is asserted directly rather
  // than assumed.
  test("in_progress -> done is suppressible, retained, clickable, and lands in Recent", () => {
    const t = task({ status: "done", target: "/somewhere" });
    const n = notificationForTransition("in_progress", t);
    expect(n?.tone).toBe("info");
    expect(n?.source).toBe("/somewhere");
    expect(n?.page).toBe(taskDestination(t));
    expect(n?.recent).toBe(true);
    expect(n?.title).toContain("finished");
  });

  test("in_progress -> blocked is a never-suppressed, retained, actioned failure", () => {
    const n = notificationForTransition(
      "in_progress",
      task({ status: "blocked", session_id: "s1", target: "/proj" }),
    );
    expect(n?.tone).toBe("error");
    expect(n?.source).toBeUndefined();
    expect(n?.page).toBe(taskDestination(task({ status: "blocked", session_id: "s1", target: "/proj" })));
    expect(n?.title).toContain("failed");
  });

  test("any transition into needs_attention is a plain, non-retained alert", () => {
    // Deliberately no tone/tier: attentionRows (tasks-lib.ts) already retains
    // this fact persistently — this notify() is only the moment-of alert, so
    // it must resolve to "transient" (pops, does not retain) rather than
    // notify()'s ordinary tone:"error" shape, which would always-retain a
    // second, duplicate row for the same task.
    const n = notificationForTransition("in_progress", task({ status: "needs_attention" }));
    expect(n?.tone).toBeUndefined();
    expect(n?.tier).toBeUndefined();
    expect(n?.page).toBeUndefined();
    expect(n?.action).toBeUndefined();
    expect(n?.title).toContain("needs your input");
  });

  test("needs_attention fires the same way regardless of the prior status", () => {
    expect(notificationForTransition("upcoming", task({ status: "needs_attention" }))?.title)
      .toContain("needs your input");
    expect(notificationForTransition("done", task({ status: "needs_attention" }))?.title)
      .toContain("needs your input");
  });

  test("a transition not named by the spec's table stays silent", () => {
    expect(notificationForTransition("upcoming", task({ status: "in_progress" }))).toBeNull();
    expect(notificationForTransition("blocked", task({ status: "in_progress" }))).toBeNull();
    expect(notificationForTransition("needs_attention", task({ status: "in_progress" }))).toBeNull();
  });

  test("falls back to a generic noun when the task has no title", () => {
    const n = notificationForTransition("in_progress", task({ status: "blocked", title: "" }));
    expect(n?.title).toBe("A task failed");
  });
});

describe("taskDestination", () => {
  test("prefers the task's own chat, then its folder, then the Tasks page", () => {
    expect(taskDestination(task({ session_id: "s1", target: "/proj" }))).toContain("/proj");
    expect(taskDestination(task({ session_id: "", target: "/proj" }))).toContain("/proj");
    expect(taskDestination(task({ session_id: "", target: "", project: "" }))).toBe("/tasks");
  });
});
