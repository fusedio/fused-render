// The Claude rail's seats (claude-rail-lib): which of the pulse's rows a
// rail draws — on a canvas, in a folder, on a file — and in what order. The
// rulings are the owner's (2026-10-28): no archived rows, and the List view's
// order rather than a sort of the rail's own; a folder's and a file's rail
// hold what their pane's Recent list holds (2026-11-01).
import { describe, expect, it } from "bun:test";
import type { Task, TaskPulseTask } from "@platform/lib/api";
import { sortForList } from "@shell/tasks-lib";
import {
  dotCleared,
  favSeats,
  keepMarks,
  onRail,
  placeName,
  placeRoot,
  railRows,
  railScope,
  readMark,
  type RailPlace,
} from "./claude-rail-lib";



const PLACE: RailPlace = { kind: "canvas", env: "unstable", name: "demo", dir: "/home/me/canvases/demo" };
const DIR = "/home/me/canvases/demo";
/** The hover card's rail: drafts and scheduled rows admitted. */
const PLACE_ALL: RailPlace = { ...PLACE, sessionless: true };

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
    project: DIR,
    target: DIR,
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
    expect(railScope({ canvas: { env: "unstable", name: "demo", file: "hex.py" }, target: "", project: "" }, PLACE))
      .toEqual({ kind: "node", name: "hex", file: "hex.py" });
    expect(railScope({ target: `${DIR}/widget_5.json`, project: DIR }, PLACE))
      .toEqual({ kind: "node", name: "widget_5", file: "widget_5.json" });
    expect(railScope({ target: DIR, project: DIR }, PLACE)).toEqual({ kind: "whole", word: "Canvas" });
    expect(railScope({ target: `${DIR}/sub/x.py`, project: DIR }, PLACE)).toEqual({ kind: "whole", word: "Canvas" });
    expect(railScope({ canvas: { env: "unstable", name: "demo", file: "" }, target: "", project: "" }, { ...PLACE, dir: null }))
      .toEqual({ kind: "whole", word: "Canvas" });
  });

  it("a FOLDER's rail holds what its pane's Recent list holds: the folder, anything under it, its project", () => {
    const FOLDER: RailPlace = { kind: "folder", dir: "/home/me/apps/demo", sessionless: true };
    const onFolder = pulse(task({ key: "a", project: "/home/me/apps/demo", target: "/home/me/apps/demo" }));
    const onFile = pulse(task({ key: "b", project: "/home/me/apps/demo", target: "/home/me/apps/demo/src/app.py" }));
    const byProject = pulse(task({ key: "c", project: "/home/me/apps/demo", target: "" }));
    const sibling = pulse(task({ key: "d", project: "/home/me/apps/demo2", target: "/home/me/apps/demo2" }));
    const archived = pulse(task({ key: "e", project: "/home/me/apps/demo", target: "/home/me/apps/demo", status: "archived" }));
    expect(onRail(onFolder, FOLDER)).toBe(true);
    expect(onRail(onFile, FOLDER)).toBe(true);
    expect(onRail(byProject, FOLDER)).toBe(true);
    // `demo2` is not under `demo`: a prefix match on the bare string would say it was.
    expect(onRail(sibling, FOLDER)).toBe(false);
    expect(onRail(archived, FOLDER)).toBe(false);
    // The card's chip: the file under the folder, else the whole, by the place's word.
    expect(railScope(onFile, FOLDER)).toEqual({ kind: "file", name: "src/app.py", file: "/home/me/apps/demo/src/app.py" });
    expect(railScope(onFolder, FOLDER)).toEqual({ kind: "whole", word: "Folder" });
    expect(railScope(onFolder, { ...FOLDER, word: "App" })).toEqual({ kind: "whole", word: "App" });
    expect(placeName(FOLDER)).toBe("demo");
    expect(placeName({ ...FOLDER, dir: "/home/me/apps/demo/" })).toBe("demo");
  });

  it("a FILE's rail holds that file's chats alone", () => {
    const FILE: RailPlace = { kind: "file", file: "/home/me/notes/plan.md", sessionless: true };
    const onFile = pulse(task({ key: "a", project: "/home/me/notes", target: "/home/me/notes/plan.md" }));
    const onFolder = pulse(task({ key: "b", project: "/home/me/notes", target: "/home/me/notes" }));
    const other = pulse(task({ key: "c", project: "/home/me/notes", target: "/home/me/notes/plan.md.bak" }));
    expect(onRail(onFile, FILE)).toBe(true);
    expect(onRail(onFolder, FILE)).toBe(false);
    expect(onRail(other, FILE)).toBe(false);
    expect(railScope(onFile, FILE)).toEqual({ kind: "whole", word: "File" });
    expect(placeName(FILE)).toBe("plan.md");
    expect(placeName(PLACE)).toBe("this canvas");
    expect(placeName({ ...PLACE, file: "hex.py" })).toBe("hex");
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
    const NODE: RailPlace = { ...PLACE, file: "udf_1.py" };
    expect(onRail(onNode, NODE)).toBe(true);
    expect(onRail(onOther, NODE)).toBe(false);
    expect(onRail(onCanvas, NODE)).toBe(false);
    // No node selected: every chat on the canvas.
    for (const t of [onNode, onOther, onCanvas]) expect(onRail(t, PLACE)).toBe(true);
  });

  it("matches an unstamped row to the selected node by its target", () => {
    const onNode = pulse(task({ key: "a", target: `${DIR}/udf_1.py` }));
    const onFolder = pulse(task({ key: "b" }));
    const NODE: RailPlace = { ...PLACE, file: "udf_1.py" };
    expect(onRail(onNode, NODE)).toBe(true);
    expect(onRail(onFolder, NODE)).toBe(false);
  });

  it("matches the selected node by NAME: the page's file is a guess from the workbench's echo", () => {
    // The echo names the node only; the page guesses `widget_5.json`, the
    // row says the widget's real file.
    const GUESS: RailPlace = { ...PLACE, file: "widget_5.json" };
    const stamped = pulse(task({ key: "a", canvas: { env: "unstable", name: "demo", file: "widget_5.html" } }));
    const legacy = pulse(task({ key: "b", target: `${DIR}/widget_5.html` }));
    const other = pulse(task({ key: "c", canvas: { env: "unstable", name: "demo", file: "widget_50.json" } }));
    const nested = pulse(task({ key: "d", target: `${DIR}/sub/widget_5.html` }));
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

// A press on a ring clears its dot at once and holds it hollow until the
// pulse agrees (owner, 2026-11-01); anything that lands after the press is
// news again, on the server's word.
describe("readMark / dotCleared / keepMarks", () => {
  const row = (unread: number, happened_at = 1_000) => ({ key: "a", unread, happened_at });

  it("a press on a row with unread leaves a mark; a hollow ring leaves none", () => {
    expect(readMark(row(2))).toEqual({ unread: 2, happened: 1_000 });
    expect(readMark(row(0))).toBeNull();
    expect(readMark({ unread: 1 })).toEqual({ unread: 1, happened: 0 });
  });

  it("the dot stays off while the row still reports what the press saw", () => {
    const mark = readMark(row(2))!;
    expect(dotCleared(mark, row(2))).toBe(true);
    expect(dotCleared(undefined, row(2))).toBe(false);
  });

  it("the server confirming (unread 0) or news after the press ends the mark", () => {
    const mark = readMark(row(2))!;
    expect(dotCleared(mark, row(0))).toBe(false);
    expect(dotCleared(mark, row(3))).toBe(false);
    // Same count, later activity: a new message replaced the read ones.
    expect(dotCleared(mark, row(2, 2_000))).toBe(false);
  });

  it("keepMarks drops spent marks and marks whose row left, and keeps the map when nothing changed", () => {
    const a = readMark(row(2))!;
    const b = { unread: 1, happened: 500 };
    const marks = new Map([
      ["a", a],
      ["b", b],
      ["gone", b],
    ]);
    const kept = keepMarks(marks, [row(2), { key: "b", unread: 0, happened_at: 500 }]);
    expect([...kept.keys()]).toEqual(["a"]);
    // Unchanged: the same map back, so a state set is a no-op.
    expect(keepMarks(kept, [row(2)])).toBe(kept);
    const empty = new Map();
    expect(keepMarks(empty, [row(2)])).toBe(empty);
  });
});

describe("placeRoot", () => {
  const FOLDER: RailPlace = { kind: "folder", dir: "/home/me/apps/demo" };
  const FILE: RailPlace = { kind: "file", file: "/home/me/notes/plan.md" };
  it("is the same canvas with or without a node selected", () => {
    expect(placeRoot({ ...PLACE, file: "hex.py" })).toBe(placeRoot(PLACE));
    expect(placeRoot({ ...PLACE, file: null })).toBe(placeRoot(PLACE));
  });
  it("tells canvases, folders and files apart", () => {
    expect(placeRoot(PLACE)).not.toBe(placeRoot({ ...PLACE, name: "other" }));
    expect(placeRoot(FOLDER)).toBe(placeRoot({ ...FOLDER, dir: FOLDER.dir + "/" }));
    expect(placeRoot(FOLDER)).not.toBe(placeRoot({ kind: "folder", dir: "/elsewhere" }));
    expect(placeRoot(FILE)).not.toBe(placeRoot(FOLDER));
  });
});

describe("favSeats", () => {
  it("lists every starred, un-archived row from anywhere, newest first, with its listing row", () => {
    const old = pulse(task({ key: "old", happened_at: 100 }), { starred: true });
    const fresh = pulse(task({ key: "fresh", happened_at: 900, project: "/elsewhere", target: "/elsewhere" }), {
      starred: true,
    });
    const archived = pulse(task({ key: "gone", status: "archived", happened_at: 950 }), { starred: true });
    const plain = pulse(task({ key: "plain", happened_at: 990 }));
    const listed = task({ key: "fresh" });
    const seats = favSeats([old, archived, plain, fresh], new Map([["fresh", listed]]));
    expect(seats.map((s) => s.pulse.key)).toEqual(["fresh", "old"]);
    expect(seats[0]!.row).toBe(listed);
    expect(seats[1]!.row).toBeNull();
  });

  it("orders ties by key so the foot does not reshuffle", () => {
    const b = pulse(task({ key: "b", happened_at: 5 }), { starred: true });
    const a = pulse(task({ key: "a", happened_at: 5 }), { starred: true });
    expect(favSeats([b, a], new Map()).map((s) => s.pulse.key)).toEqual(["a", "b"]);
  });
});
