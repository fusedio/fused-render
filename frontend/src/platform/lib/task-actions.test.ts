import { expect, test } from "bun:test";
import {
  RUN_BLOCKED_HINT,
  TASK_ACTION_LABELS,
  blockedActionLabel,
  terminalActionLabel,
} from "./task-actions";

test("the terminal hand-off names the agent only when there is more than one", () => {
  expect(terminalActionLabel(null, true)).toBe("Continue in terminal");
  expect(terminalActionLabel(null, false)).toBe("New session in terminal");
  expect(terminalActionLabel("Copilot", true)).toBe("Continue with Copilot in terminal");
  expect(terminalActionLabel("Copilot", false)).toBe("New Copilot session in terminal");
});

test("a refused row says why in its label, lower-cased after the dash", () => {
  expect(blockedActionLabel(TASK_ACTION_LABELS.delete, RUN_BLOCKED_HINT)).toBe(
    "Delete task — stop the run first",
  );
  expect(blockedActionLabel(TASK_ACTION_LABELS.archive, "Finish or discard the notes first")).toBe(
    "Archive task — finish or discard the notes first",
  );
  expect(blockedActionLabel(TASK_ACTION_LABELS.delete, null)).toBe("Delete task");
});

import { TASKS_PATH, isTasksPath, openInTasksHref } from "./task-actions";

test("Open in Tasks: href for a normal task, null on the Tasks page / flag off / no row", () => {
  const base = { on: true, taskKey: "abc 123", pathname: "/explorer/view/x" };
  expect(TASK_ACTION_LABELS.openInTasks).toBe("Open in Tasks");
  expect(openInTasksHref(base)).toBe("/tasks?peek=abc%20123");
  expect(openInTasksHref({ ...base, pathname: "/tasks" })).toBeNull();
  expect(openInTasksHref({ ...base, on: false })).toBeNull();
  expect(openInTasksHref({ ...base, taskKey: "" })).toBeNull();
  expect(openInTasksHref({ ...base, taskKey: null })).toBeNull();
  expect(isTasksPath(TASKS_PATH)).toBe(true);
  expect(isTasksPath("/tasks/other")).toBe(false);
});
