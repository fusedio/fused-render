// The Claude rail's seats (claude-rail-lib): which of the pulse's rows a
// canvas's rail draws, and in what order. Both rulings are the owner's
// (2026-10-28): no archived rows, and the List view's order rather than a
// sort of the rail's own.
import { describe, expect, it } from "bun:test";
import type { Task, TaskPulseTask } from "@platform/lib/api";
import { sortForList } from "@shell/tasks-lib";
import { onRail, railRows } from "./claude-rail-lib";

const PLACE = { env: "unstable" as const, name: "demo", dir: "/home/me/canvases/demo" };

/** A full task row with every field the rail's order can read set to a
 *  harmless default; the test states only what it is about. */
function task(over: Partial<Task> & { key: string }): Task {
  return {
    task_id: over.key,
    session_id: `s-${over.key}`,
    status: "done",
    unread: 0,
    last_active: 1_000,
    happened_at: 1_000,
    project: PLACE.dir,
    target: PLACE.dir,
    title: over.key,
    ...over,
  } as Task;
}

function pulse(t: Task, over: Partial<TaskPulseTask> = {}): TaskPulseTask {
  return { ...t, ...over } as TaskPulseTask;
}

describe("onRail", () => {
  it("keeps a chat on this canvas and drops an archived one", () => {
    const live = pulse(task({ key: "a", status: "in_progress" }));
    const archived = pulse(task({ key: "b", status: "archived" }));
    expect(onRail(live, PLACE)).toBe(true);
    expect(onRail(archived, PLACE)).toBe(false);
  });

  it("drops a row with no session, and one on another canvas", () => {
    const noSession = pulse(task({ key: "a", session_id: "" }));
    const elsewhere = pulse(task({ key: "b", project: "/elsewhere", target: "/elsewhere" }));
    expect(onRail(noSession, PLACE)).toBe(false);
    expect(onRail(elsewhere, PLACE)).toBe(false);
  });

  it("matches by `task.canvas` when the row carries it", () => {
    const named = pulse(
      task({ key: "a", project: "/elsewhere", canvas: { env: "unstable", name: "demo" } } as Partial<Task> & {
        key: string;
      }),
    );
    expect(onRail(named, PLACE)).toBe(true);
    expect(onRail(named, { ...PLACE, name: "other" })).toBe(false);
  });
});

describe("railRows", () => {
  const now = Date.parse("2026-10-28T12:00:00Z");

  it("is the List's order, not newest-first", () => {
    // A Done chat touched a minute ago, and a running one touched an hour
    // ago: newest-first put Done on top; the List puts the running one first
    // (In Progress is the first lane).
    const done = task({ key: "done", status: "done", last_active: 5_000 });
    const running = task({ key: "run", status: "in_progress", last_active: 2_000 });
    const listing = new Map([done, running].map((t) => [t.key, t]));
    const seats = railRows([pulse(done), pulse(running)], listing, PLACE, now);
    expect(seats.map((s) => s.pulse.key)).toEqual(
      sortForList([done, running], now).map((t) => t.key),
    );
    expect(seats[0]?.pulse.key).toBe("run");
  });

  it("ranks by the pulse's status, which is fresher than the listing's", () => {
    const stale = task({ key: "a", status: "in_progress", last_active: 2_000 });
    const other = task({ key: "b", status: "in_progress", last_active: 1_000 });
    const listing = new Map([stale, other].map((t) => [t.key, t]));
    // The pulse says `a` finished: it moves under the still-running `b`.
    const seats = railRows([pulse(stale, { status: "done" }), pulse(other)], listing, PLACE, now);
    expect(seats.map((s) => s.pulse.key)).toEqual(["b", "a"]);
    expect(seats[1]?.row?.key).toBe("a");
  });

  it("leaves archived rows out even when the listing still holds them", () => {
    const live = task({ key: "a", status: "in_progress" });
    const gone = task({ key: "b", status: "in_progress" });
    const listing = new Map([live, gone].map((t) => [t.key, t]));
    const seats = railRows([pulse(live), pulse(gone, { status: "archived" })], listing, PLACE, now);
    expect(seats.map((s) => s.pulse.key)).toEqual(["a"]);
  });

  it("ranks rows the listing does not hold yet in their lane, not at the head", () => {
    const held = task({ key: "held", status: "in_progress", last_active: 1_000 });
    const listing = new Map([[held.key, held]]);
    const newer = pulse(task({ key: "new2", status: "in_progress", last_active: 4_000 }));
    const older = pulse(task({ key: "new1", status: "in_progress", last_active: 3_000 }));
    // A chat that finished inside the listing's refresh window: Done, so
    // under every running row, listed or not. In Progress itself keeps the
    // server's order (tasks-lib LANE_SORTS), so the running rows read as the
    // pulse sent them.
    const justDone = pulse(task({ key: "fin", status: "done", last_active: 9_000 }));
    const seats = railRows([pulse(held), older, justDone, newer], listing, PLACE, now);
    expect(seats.map((s) => s.pulse.key)).toEqual(["held", "new1", "new2", "fin"]);
    expect(seats[0]?.row?.key).toBe("held");
    expect(seats[1]?.row).toBeNull();
    expect(seats[3]?.row).toBeNull();
  });
});
