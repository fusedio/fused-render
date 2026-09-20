// TaskPeekWho: the header's title follows the same rule as the row (Akshil,
// 2026-09-15: "show the same title everywhere") — the task's own title, first
// line only, since the "title by your last message" switch went (2026-09-20).
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

test("the task's title, first line only, whatever was said", () => {
  const v = render({ task: said("do the thing") });
  expect(v.text()).toContain("Rename the pane noun");
  expect(v.text()).not.toContain("do the thing");
  const multi = render({ task: task({ title: "First line\nSecond line" }) });
  expect(multi.text()).toContain("First line");
  expect(multi.text()).not.toContain("Second line");
});

test("a task with no title prints the wall's own word", () => {
  expect(render({ task: task({ title: "" }) }).text()).toContain("(untitled)");
});
