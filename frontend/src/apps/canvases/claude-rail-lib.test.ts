// The Claude rail's seats (claude-rail-lib): which of the pulse's rows a
// canvas's rail draws, and in what order. Both rulings are the owner's
// (2026-10-28): no archived rows, and the List view's order rather than a
// sort of the rail's own.
import { describe, expect, it } from "bun:test";
import type { Task, TaskPulseTask } from "@platform/lib/api";
import { sortForList } from "@shell/tasks-lib";
import { onRail, railRows, railScope } from "./claude-rail-lib";

const PLACE = { env: "unstable" as const, name: "demo", dir: "/home/me/canvases/demo" };
/** The hover card's rail: drafts and scheduled rows admitted. */
const PLACE_ALL = { ...PLACE, sessionless: true };

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

  it("a row with no session is on the rail only when the host admits them (the hover card); another canvas never is", () => {
    const noSession = pulse(task({ key: "a", session_id: "" }));
    expect(onRail(noSession, PLACE)).toBe(false);
    expect(onRail(noSession, PLACE_ALL)).toBe(true);
    const elsewhere = pulse(task({ key: "b", project: "/elsewhere", target: "/elsewhere" }));
    expect(onRail(elsewhere, PLACE)).toBe(false);
  });

  it("a draft the pulse does not carry comes from the listing, ranked like the List", () => {
    const draft = task({ key: "draft:1", kind: "draft", status: "upcoming", session_id: "" } as Partial<Task> & { key: string });
    const live = task({ key: "a", status: "in_progress" });
    const seats = railRows([pulse(live)], new Map([["draft:1", draft], ["a", live]]), PLACE_ALL);
    expect(seats.map((s) => s.pulse.key)).toEqual(sortForList([live, draft], Date.now()).map((t) => t.key));
    expect(seats.find((s) => s.pulse.key === "draft:1")?.row).toBe(draft);
    // The plain rail (flag off) is chats only, as it always was.
    expect(railRows([pulse(live)], new Map([["draft:1", draft], ["a", live]]), PLACE).map((s) => s.pulse.key)).toEqual(["a"]);
  });

  it("railScope: a stamped node file, a legacy target under the clone, else the canvas", () => {
    expect(railScope({ canvas: { env: "unstable", name: "demo", file: "hex.py" }, target: "", project: "" }, PLACE.dir))
      .toEqual({ kind: "node", name: "hex", file: "hex.py" });
    expect(railScope({ target: `${PLACE.dir}/widget_5.json`, project: PLACE.dir }, PLACE.dir))
      .toEqual({ kind: "node", name: "widget_5", file: "widget_5.json" });
    expect(railScope({ target: PLACE.dir, project: PLACE.dir }, PLACE.dir)).toEqual({ kind: "canvas" });
    expect(railScope({ target: `${PLACE.dir}/sub/x.py`, project: PLACE.dir }, PLACE.dir)).toEqual({ kind: "canvas" });
    expect(railScope({ canvas: { env: "unstable", name: "demo", file: "" }, target: "", project: "" }, null))
      .toEqual({ kind: "canvas" });
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

  it("holds only the selected node's chats while a node is selected", () => {
    const stamp = (key: string, file: string) =>
      pulse(task({ key, canvas: { env: "unstable", name: "demo", file } }));
    const onNode = stamp("a", "udf_1.py");
    const onOther = stamp("b", "udf_2.py");
    const onCanvas = stamp("c", "");
    const NODE = { ...PLACE, file: "udf_1.py" };
    expect(onRail(onNode, NODE)).toBe(true);
    expect(onRail(onOther, NODE)).toBe(false);
    expect(onRail(onCanvas, NODE)).toBe(false);
    // No node selected: every chat on the canvas.
    for (const t of [onNode, onOther, onCanvas]) expect(onRail(t, PLACE)).toBe(true);
  });

  it("matches an unstamped row to the selected node by its target", () => {
    const onNode = pulse(task({ key: "a", target: `${PLACE.dir}/udf_1.py` }));
    const onFolder = pulse(task({ key: "b" }));
    const NODE = { ...PLACE, file: "udf_1.py" };
    expect(onRail(onNode, NODE)).toBe(true);
    expect(onRail(onFolder, NODE)).toBe(false);
  });

  it("matches the selected node by NAME: the page's file is a guess from the workbench's echo", () => {
    // The echo names the node only; the page guesses `widget_5.json`, the
    // row says the widget's real file.
    const GUESS = { ...PLACE, file: "widget_5.json" };
    const stamped = pulse(task({ key: "a", canvas: { env: "unstable", name: "demo", file: "widget_5.html" } }));
    const legacy = pulse(task({ key: "b", target: `${PLACE.dir}/widget_5.html` }));
    const other = pulse(task({ key: "c", canvas: { env: "unstable", name: "demo", file: "widget_50.json" } }));
    const nested = pulse(task({ key: "d", target: `${PLACE.dir}/sub/widget_5.html` }));
    expect(onRail(stamped, GUESS)).toBe(true);
    expect(onRail(legacy, GUESS)).toBe(true);
    expect(onRail(other, GUESS)).toBe(false);
    expect(onRail(nested, GUESS)).toBe(false);
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
