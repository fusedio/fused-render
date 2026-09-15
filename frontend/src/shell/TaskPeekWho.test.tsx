// TaskPeekWho: the header's title follows the same rule as the row (Akshil,
// 2026-09-15: "header also shows last message"). Lives in shell/ because the
// pref module is shell's and apps/claude may not import it (check-boundaries).
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, expect, test } from "bun:test";
import { createElement } from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";

import type { Task } from "@platform/lib/api";

const { TaskPeekWho } = await import("./TaskPeekWho");
const { publishTaskCardTitleMode } = await import("./task-card-title-flag");
// PUBLISHED, NOT LEFT TO THE READ: the hook otherwise asks `/api/prefs` on
// mount and settles after the render — a state update outside `act`. A publish
// stands in for the answer and stops the read. OFF is the shipping default.
publishTaskCardTitleMode(false);

const mounted: ReactTestRenderer[] = [];
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
  act(() => publishTaskCardTitleMode(false));
});

const task = (over: Partial<Task> = {}): Task =>
  ({
    key: "sess-1",
    task_id: "TASK-042",
    title: "Rename the pane noun",
    project: "/repo/app",
    target: "/repo/app/x.py",
    session_id: "sess-1",
    status: "done",
    ...over,
  }) as Task;

function render(props: Parameters<typeof TaskPeekWho>[0]) {
  let r!: ReactTestRenderer;
  act(() => {
    r = create(createElement(TaskPeekWho, props));
  });
  mounted.push(r);
  return { text: () => JSON.stringify(r.toJSON()) };
}

const said = (text: string) =>
  task({ last_message: { role: "assistant", text, at: 1 } } as Partial<Task>);

test("pref ON: the header prints the row's line — the newest message, first line only", () => {
  act(() => publishTaskCardTitleMode(true));
  const v = render({ task: said("Renamed it.\nMore below") });
  expect(v.text()).toContain("Renamed it.");
  expect(v.text()).not.toContain("More below");
  expect(v.text()).not.toContain("Rename the pane noun");
});

test("pref ON, nothing said yet: the task's own name, as the row does", () => {
  act(() => publishTaskCardTitleMode(true));
  expect(render({ task: task() }).text()).toContain("Rename the pane noun");
});

test("pref OFF: the task's title, whatever was said", () => {
  const v = render({ task: said("do the thing") });
  expect(v.text()).toContain("Rename the pane noun");
  expect(v.text()).not.toContain("do the thing");
});

test("the pref flipping while the header is up repaints it in place", () => {
  const v = render({ task: said("Renamed it.") });
  expect(v.text()).toContain("Rename the pane noun");
  act(() => publishTaskCardTitleMode(true));
  expect(v.text()).toContain("Renamed it.");
});
