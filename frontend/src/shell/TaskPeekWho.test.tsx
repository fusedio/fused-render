// TaskPeekWho: the header's title follows the same rule as the row (Akshil,
// 2026-09-15: "header also shows last message") — the reader's newest message,
// first line only, else the task's own title. Always on since the "title by
// your last message" switch went (2026-09-20).
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, expect, test } from "bun:test";
import { createElement } from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";

import type { Task } from "@platform/lib/api";

const { TaskPeekWho } = await import("./TaskPeekWho");

const mounted: ReactTestRenderer[] = [];
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
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

// The server only ever sends the reader's own prompt as `last_message` now;
// the client does not read `role`, so the fixture keeps the wire shape only.
const said = (text: string) =>
  task({ last_message: { role: "user", text, at: 1 } } as Partial<Task>);

test("the header prints the row's line — the newest message, first line only", () => {
  const v = render({ task: said("Renamed it.\nMore below") });
  expect(v.text()).toContain("Renamed it.");
  expect(v.text()).not.toContain("More below");
  expect(v.text()).not.toContain("Rename the pane noun");
});

test("nothing said yet: the task's own name, first line only, as the row does", () => {
  expect(render({ task: task() }).text()).toContain("Rename the pane noun");
  const multi = render({ task: task({ title: "First line\nSecond line" }) });
  expect(multi.text()).toContain("First line");
  expect(multi.text()).not.toContain("Second line");
});

test("a task with no title and nothing said prints the wall's own word", () => {
  expect(render({ task: task({ title: "" }) }).text()).toContain("(untitled)");
});
