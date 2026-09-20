// A PRESS ON THE QUEUE CAPTION OPENS THE ROW'S OWN CHAT (Akshil, 2026-09-19).
//
// THE BUG. A queued row's caption (`.tasks-row-queue`) is `position: relative;
// z-index: 2` in tasks.css — it has to be, so its tooltip is not clipped by the
// row — and the row's navigation is an `<a>` STRETCHED OVER the whole row at
// `z-index: 1` (`.tasks-rowlink`). So every pixel the caption covered was a dead
// run: on the Tasks page and in Recent chats alike, clicking "after TASK-046 |
// 3rd" did nothing at all, while clicking the title one character to its left
// opened the conversation. It is the identical fault the file mark beside it was
// fixed for on 2026-08-27, on the identical cause.
//
// WHY A RENDER TEST AND NOT A SOURCE-STRING ONE. The rest of this feature is
// pinned by reading source (project-queue.test.ts), which is cheap and says
// nothing about whether a handler is REACHED: the whole failure here was markup
// that looked right sitting on top of a link. So this one mounts the row and
// dispatches a press the way React would — inner handler first, outward until
// something stops it.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();

import { afterEach, expect, test } from "bun:test";
import { createElement } from "react";
import { act, create, type ReactTestRenderer, type ReactTestInstance } from "react-test-renderer";

import type { Task } from "@platform/lib/api";

const { TaskRowItem } = await import("./ScheduleTaskViews");

const mounted: ReactTestRenderer[] = [];
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
});

const HOLDER = {
  queue_ahead: "TASK-046",
  queue_ahead_title: "Nightly deploy",
  queue_ahead_session: "sess-46",
  queue_ahead_target: "/repo/alpha/deploy.py",
};

const queued = (over: Partial<Task> = {}): Task =>
  ({
    key: "sess-1",
    task_id: "TASK-099",
    title: "say hi",
    project: "/repo/alpha",
    target: "/repo/alpha/x.py",
    session_id: "sess-1",
    status: "queued",
    queue_position: 3,
    ...HOLDER,
    ...over,
  }) as Task;

function row(task: Task, opened: string[]) {
  let r!: ReactTestRenderer;
  act(() => {
    r = create(
      createElement(TaskRowItem, {
        task,
        href: "/explorer/view/repo/alpha/x.py?_side=claude&session_id=sess-1",
        onPress: () => opened.push(task.key),
      }),
    );
  });
  mounted.push(r);
  return r;
}

function byClass(r: ReactTestRenderer, cls: string): ReactTestInstance[] {
  return r.root.findAll(
    (n) =>
      typeof n.type === "string" &&
      String((n.props as { className?: string }).className ?? "")
        .split(/\s+/)
        .includes(cls),
  );
}

/** Every string the row prints, joined. */
function text(r: ReactTestRenderer): string {
  const out: string[] = [];
  const walk = (node: unknown): void => {
    if (typeof node === "string") return void out.push(node);
    if (Array.isArray(node)) return void node.forEach(walk);
    if (node && typeof node === "object" && "children" in node) {
      walk((node as { children: unknown }).children);
    }
  };
  walk(r.toJSON() as unknown);
  return out.join("");
}

/**
 * A CLICK, DISPATCHED THE WAY THE BROWSER AND REACT WOULD — on `from`, then on
 * each ancestor out to the tree's root, stopping the moment a handler calls
 * `stopPropagation`. `react-test-renderer` calls only the handler you reach for,
 * so bubbling is the thing a test of "does the press reach the row" has to
 * supply itself; it is also the only mechanism the link's `stopPropagation` can
 * possibly be tested through.
 */
function click(from: ReactTestInstance) {
  let stopped = false;
  const ev = {
    button: 0,
    metaKey: false,
    ctrlKey: false,
    shiftKey: false,
    altKey: false,
    preventDefault() {},
    stopPropagation() {
      stopped = true;
    },
  };
  act(() => {
    for (let at: ReactTestInstance | null = from; at && !stopped; at = at.parent ?? null) {
      const onClick = (at.props as { onClick?: (e: unknown) => void }).onClick;
      if (typeof at.type === "string" && onClick) onClick(ev);
    }
  });
}

test("the caption's ORDINARY text opens the row's own chat", () => {
  // The words that are not the id are the row, exactly as the title is: a
  // reader aiming at a queued row's sentence is aiming at that row.
  const opened: string[] = [];
  const r = row(queued(), opened);
  const caption = byClass(r, "tasks-queue-text")[0]!;
  expect(caption).toBeDefined();
  click(caption);
  expect(opened).toEqual(["sess-1"]);
});

test("…and so does the caption's own box, which sits OVER the stretched link", () => {
  // `.tasks-row-queue` is the element with the `z-index` that caused the bug, so
  // it is the element the handlers have to be on — a press landing on its
  // padding rather than on the words must open the row too.
  const opened: string[] = [];
  const r = row(queued(), opened);
  click(byClass(r, "tasks-row-queue")[0]!);
  expect(opened).toEqual(["sess-1"]);
});

test("the TASK-x link opens the HOLDER and fires nothing else", () => {
  const opened: string[] = [];
  const r = row(queued(), opened);
  const link = r.root.findAll(
    (n) => n.type === "a" && String((n.props as { className?: string }).className ?? "")
      .includes("tasks-queue-ahead"),
  );
  expect(link).toHaveLength(1);
  // Where it goes is the holder's conversation and not this row's.
  expect(String(link[0]!.props.href)).toContain("session_id=sess-46");
  expect(String(link[0]!.props.href)).toContain("deploy.py");
  expect(link[0]!.props.title).toBe("Nightly deploy");
  // And the press stops there: the row it sits in must not ALSO open.
  click(link[0]!);
  expect(opened).toEqual([]);
});

test("the sentence is the one lingo: `after TASK-046 | 3rd`", () => {
  expect(text(row(queued(), []))).toContain("after TASK-046 | 3rd");
});

test("a skipped row is drawn exactly like every other waiting row", () => {
  // A skip changes the ORDER and nothing else, and the order is already the
  // sentence. No ⤒ in the caption, no second colour, no `is-next`.
  const r = row(queued({ queue_priority: true, queue_position: 1 } as Partial<Task>), []);
  const said = JSON.stringify(r.toJSON());
  expect(byClass(r, "tasks-queue-glyph")).toHaveLength(0);
  expect(said).not.toContain("is-next");
  expect(text(r)).toContain("after TASK-046 | 1st");
  // The ⤒ that IS still drawn is the Run next BUTTON's face, which is an action
  // and not a highlight — and at the head of the line it is disabled, not gone.
  const skip = byClass(r, "tasks-act--skip");
  expect(skip).toHaveLength(1);
  expect(skip[0]!.props.disabled).toBe(true);
});
