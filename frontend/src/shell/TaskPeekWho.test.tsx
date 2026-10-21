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
  return { r, text: () => JSON.stringify(r.toJSON()) };
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

// ---- renaming in place (the title is the reader's to change) -----------------

const titleSpan = (r: ReactTestRenderer) =>
  r.root.findAll((n) => n.type === "span" && n.props.className === "task-side-peek-title")[0];
const inputOf = (r: ReactTestRenderer) =>
  r.root.findAll((n) => n.type === "input" && n.props.className === "task-side-peek-title-input")[0];
const pencilOf = (r: ReactTestRenderer) =>
  r.root.findAll((n) => n.type === "button" && n.props["aria-label"] === "Rename task")[0];
const edit = (r: ReactTestRenderer, value: string) =>
  act(() => {
    inputOf(r).props.onChange({ target: { value } });
  });
const key = (r: ReactTestRenderer, k: string) =>
  act(() => {
    inputOf(r).props.onKeyDown({ key: k, preventDefault() {} });
  });

test("double-click opens an input prefilled with the title; Enter saves the trimmed text", async () => {
  const calls: [string, string][] = [];
  const v = render({ task: task(), rename: async (k, t) => void calls.push([k, t]) });
  expect(inputOf(v.r)).toBeUndefined();
  act(() => {
    titleSpan(v.r).props.onDoubleClick();
  });
  expect(inputOf(v.r).props.value).toBe("Rename the pane noun");
  edit(v.r, "  Better name  ");
  await act(async () => {
    key(v.r, "Enter");
  });
  expect(calls).toEqual([["sess-1", "Better name"]]);
  expect(inputOf(v.r)).toBeUndefined();
});

test("the pencil button opens the editor too", () => {
  const v = render({ task: task(), rename: async () => {} });
  act(() => {
    pencilOf(v.r).props.onClick();
  });
  expect(inputOf(v.r)).toBeDefined();
});

test("Escape cancels and the unchanged text saves nothing", async () => {
  const calls: string[] = [];
  const v = render({
    task: task({ title_source: "renamed" } as Partial<Task>),
    rename: async (_k, t) => void calls.push(t),
  });
  act(() => {
    titleSpan(v.r).props.onDoubleClick();
  });
  edit(v.r, "Something else");
  key(v.r, "Escape");
  expect(inputOf(v.r)).toBeUndefined();
  act(() => {
    titleSpan(v.r).props.onDoubleClick();
  });
  await act(async () => {
    key(v.r, "Enter");
  });
  expect(calls).toEqual([]);
  expect(inputOf(v.r)).toBeUndefined();
});

test("a failed save keeps the input open and says why", async () => {
  const v = render({
    task: task(),
    rename: async () => {
      throw new Error("boom");
    },
  });
  act(() => {
    titleSpan(v.r).props.onDoubleClick();
  });
  edit(v.r, "New");
  await act(async () => {
    key(v.r, "Enter");
  });
  expect(inputOf(v.r)).toBeDefined();
  expect(v.text()).toContain("boom");
});

test("a task that has not started offers no rename", () => {
  for (const k of ["pending:e1", "draft:d1", "new:abc"]) {
    const v = render({ task: task({ key: k }), rename: async () => {} });
    expect(pencilOf(v.r)).toBeUndefined();
    act(() => {
      titleSpan(v.r).props.onDoubleClick?.();
    });
    expect(inputOf(v.r)).toBeUndefined();
  }
});

// ---- the no-op check compares the STORED name, not the displayed headline ----

async function saveUntouched(
  t: Task,
  value?: string,
): Promise<{ calls: string[]; v: ReturnType<typeof render> }> {
  const calls: string[] = [];
  const v = render({ task: t, rename: async (_k, x) => void calls.push(x) });
  act(() => {
    titleSpan(v.r).props.onDoubleClick();
  });
  if (value !== undefined) edit(v.r, value);
  await act(async () => {
    key(v.r, "Enter");
  });
  return { calls, v };
}

test("not renamed, headline is the last message: saving it unchanged pins it as the name", async () => {
  const t = task({
    title_source: "message",
    last_message: { role: "user", text: "Newest message", at: 1 },
  } as Partial<Task>);
  const { calls } = await saveUntouched(t);
  expect(calls).toEqual(["Newest message"]);
});

test("renamed: saving the stored name unchanged calls nothing", async () => {
  const { calls } = await saveUntouched(
    task({ title: "Mine", title_source: "renamed" } as Partial<Task>),
  );
  expect(calls).toEqual([]);
});

test("renamed: clearing the field saves an empty name", async () => {
  const { calls } = await saveUntouched(
    task({ title: "Mine", title_source: "renamed" } as Partial<Task>),
    "",
  );
  expect(calls).toEqual([""]);
});

test("not renamed: clearing the field has nothing to clear", async () => {
  const { calls } = await saveUntouched(task({ title_source: "message" } as Partial<Task>), "");
  expect(calls).toEqual([]);
});
