// WHAT A QUEUED ROW DRAWS — and, since 2026-09-21, what it does NOT.
//
// THE ROW PRINTS NO PLACE (Akshil, 2026-09-21). "after TASK-046 | 3rd" used to
// sit at the right end of every waiting row and card; the dashed ring and the
// `queued` word already say the state, and where in the line a row stands is a
// detail of one row rather than a column. The sentence survives where it is
// read on purpose — the chat's waiting card over the composer and the chat
// header — and this file is what keeps it off the rows.
//
// WHAT IS LEFT HERE IS THE PRESS: Force start, on every waiting row including
// the head of the line, reachable and posting to the one endpoint.
//
// WHY A RENDER TEST AND NOT A SOURCE-STRING ONE. The rest of this feature is
// pinned by reading source (project-queue.test.ts), which is cheap and says
// nothing about whether a control is REACHED. So this one mounts the row and
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

test("a waiting row prints NO place and NO holder", () => {
  // The caption is gone from the row, ink and element both: no ordinal, no
  // "after TASK-046", and none of the markup that carried them (`.tasks-row-queue`
  // survives in this file only as the usage limit's seat, which is a different
  // sentence on a different row).
  const r = row(queued(), []);
  expect(text(r)).not.toContain("after TASK-046");
  expect(text(r)).not.toContain("3rd");
  expect(byClass(r, "tasks-queue-text")).toHaveLength(0);
  expect(
    r.root.findAll(
      (n) => n.type === "a" && String((n.props as { className?: string }).className ?? "")
        .includes("tasks-queue-ahead"),
    ),
  ).toHaveLength(0);
});

test("a promoted row is drawn exactly like every other waiting row", () => {
  // A promotion changes the ORDER and nothing else, and the order is already
  // the sentence. No ⤒ in the caption, no second colour, no `is-next`.
  const r = row(queued({ queue_priority: true, queue_position: 1 } as Partial<Task>), []);
  const said = JSON.stringify(r.toJSON());
  expect(byClass(r, "tasks-queue-glyph")).toHaveLength(0);
  expect(said).not.toContain("is-next");
  // …AND NO ⤒ ANYWHERE ELSE ON THE ROW EITHER (Akshil, 2026-09-21, glyph gone
  // for good 2026-09-22). It was the Run next button's face, and Run next is
  // out of the code entirely — so a row promoted by a Run now that had to wait,
  // which still carries `queue_priority` in the index, comes back as an
  // ordinary waiting row wearing nothing.
  expect(said).not.toContain("⤒");
  // The seat itself is NOT empty: Force start wears it (`.tasks-act--force` is
  // that seat's class, kept with its skin — see styles/tasks.css). What a
  // promoted row must not grow is a SECOND mark in the caption, which is what
  // the assertions above are about.
  expect(byClass(r, "tasks-act--force")).toHaveLength(1);
});

test("a QUEUED ROW OFFERS NO RUN NEXT, mounted (Akshil, 2026-09-21)", () => {
  // The source guard is read in project-queue.test.ts; this is the one that says
  // the control is not REACHABLE — the row is mounted with the queue on, at the
  // position (`3`) that used to grow the button, and counted.
  const r = row(queued(), []);
  expect(text(r)).not.toContain("Run next");
  expect(text(r)).not.toContain("⤒");
  const labelled = r.root.findAll(
    (n) =>
      n.type === "button" &&
      String((n.props as { "aria-label"?: string })["aria-label"] ?? "").includes("Run next"),
  );
  expect(labelled).toHaveLength(0);
});

/** Every Force start button on this row, found the way a reader finds it: by the
 *  name the label reads out. */
function forceButtons(r: ReactTestRenderer): ReactTestInstance[] {
  return r.root.findAll(
    (n) =>
      n.type === "button" &&
      String((n.props as { "aria-label"?: string })["aria-label"] ?? "").startsWith(
        "Force start",
      ),
  );
}

test("a queued row offers FORCE START at the head of the line too, mounted", () => {
  // POSITION 1 IS THE CASE (Akshil, 2026-09-21): it is the arrangement Run next
  // was hidden in, and the one this verb exists for — a row standing 1st is
  // still waiting on a turn that may have an hour left in it.
  const r = row(queued({ queue_position: 1 } as Partial<Task>), []);
  const buttons = forceButtons(r);
  expect(buttons).toHaveLength(1);
  expect(buttons[0]!.props["aria-label"]).toBe("Force start for TASK-099");
  expect(buttons[0]!.props.title).toBe("Run immediately");
  // THE SEAT SAYS THE WORDS, not a glyph: the strip beside it already holds a
  // play triangle, and two start-ish shapes one press apart is a hover a reader
  // should not have to spend (Akshil, 2026-09-21).
  expect(text(r)).toContain("Force start");
  // …and at 3rd as well, which is the only position the old verb had.
  expect(forceButtons(row(queued(), []))).toHaveLength(1);
});

test("a row that is NOT queued offers it nowhere", () => {
  // Hidden rather than disabled: a control that is present-but-dead on every row
  // is what makes the rows it works on hard to find.
  for (const status of ["in_progress", "done", "blocked", "upcoming"]) {
    const r = row(queued({ status, queue_position: 0 } as Partial<Task>), []);
    expect(forceButtons(r)).toHaveLength(0);
  }
  // …AND NEITHER DOES A QUEUED ROW THE SERVER PLACED NOWHERE (`canForceStart`):
  // position 0 is not a claim that this is standing in a line.
  expect(forceButtons(row(queued({ queue_position: 0 } as Partial<Task>), []))).toHaveLength(
    0,
  );
});

test("the press posts to /api/tasks/queue/force, and names this row's task", async () => {
  const calls: Array<{ url: string; body: Record<string, unknown> }> = [];
  const realFetch = globalThis.fetch;
  (globalThis as { fetch: unknown }).fetch = async (url: string, init: RequestInit) => {
    calls.push({ url, body: JSON.parse(String(init.body)) as Record<string, unknown> });
    return {
      ok: true,
      json: async () => ({ ok: true, started: true, run_id: "r1", session_id: "s1" }),
    } as unknown as Response;
  };
  try {
    const r = row(queued({ queue_position: 1 } as Partial<Task>), []);
    click(forceButtons(r)[0]!);
    // The call is awaited inside the handler, so let the microtask queue drain.
    await act(async () => {});
    expect(calls).toHaveLength(1);
    expect(calls[0]!.url).toBe("/api/tasks/queue/force");
    // BY TASK KEY on a row that IS a task — the server resolves that task's
    // oldest due message itself (`performForceStart`).
    expect(calls[0]!.body).toEqual({ key: "sess-1" });
  } finally {
    (globalThis as { fetch: unknown }).fetch = realFetch;
  }
});
