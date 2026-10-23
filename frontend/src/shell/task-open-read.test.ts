// An open task is never unread (exception 1 of the Workspace scope): the pure
// half. Which tasks are "open and visible" right now, and which of them still
// carry unread messages that should be marked as they arrive.
import { describe, expect, test } from "bun:test";
import type { TaskPulseTask } from "@platform/lib/api";
import { openTaskKeys, unreadOpenKeys } from "@shell/task-open-read";

function task(over: Partial<TaskPulseTask> = {}): TaskPulseTask {
  return {
    key: "s1",
    status: "done",
    unread: 0,
    last_active: 0,
    happened_at: 0,
    project: "/proj",
    task_id: "T001",
    title: "t",
    target: "/proj",
    session_id: "s1",
    ...over,
  } as TaskPulseTask;
}

describe("openTaskKeys", () => {
  const rows = [task(), task({ key: "s2", session_id: "s2" })];

  test("the peeked task is open while the document is visible", () => {
    expect(openTaskKeys(rows, { peekedKey: "s2", search: "", visible: true })).toEqual(["s2"]);
  });

  test("a hidden window has no open task", () => {
    expect(openTaskKeys(rows, { peekedKey: "s2", search: "", visible: false })).toEqual([]);
  });

  test("a full chat page names its task by session_id in the URL", () => {
    expect(
      openTaskKeys(rows, { peekedKey: null, search: "?_side=claude&session_id=s1", visible: true }),
    ).toEqual(["s1"]);
  });

  test("a task with no session never matches an empty session_id param", () => {
    const bare = [task({ key: "pending:x", session_id: "" })];
    expect(openTaskKeys(bare, { peekedKey: null, search: "?session_id=", visible: true })).toEqual([]);
  });

  test("nothing open, nothing returned", () => {
    expect(openTaskKeys(rows, { peekedKey: null, search: "", visible: true })).toEqual([]);
  });
});

describe("unreadOpenKeys", () => {
  test("only open tasks with unread messages are returned", () => {
    const rows = [task({ unread: 2 }), task({ key: "s2", session_id: "s2", unread: 3 })];
    expect(unreadOpenKeys(rows, ["s1"])).toEqual(["s1"]);
  });

  test("an open task with nothing unread is left alone", () => {
    expect(unreadOpenKeys([task({ unread: 0 })], ["s1"])).toEqual([]);
  });
});
