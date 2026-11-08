// The Claude rail's seats (claude-rail-lib): which of the pulse's rows a
// rail draws — on a canvas, in a folder, on a file — and in what order. The
// rulings are the owner's (2026-10-28): no archived rows, and the List view's
// order rather than a sort of the rail's own; a folder's and a file's rail
// hold what their pane's Recent list holds (2026-11-01).
import { describe, expect, it } from "bun:test";
import type { Task, TaskPulseTask } from "@platform/lib/api";
import { sortForList } from "@shell/tasks-lib";
import {
  activeRing,
  chipDoor,
  chipPath,
  placeTags,
  chipLabel,
  chipPlace,
  dotCleared,
  favSeats,
  listHidden,
  plusShown,
  pinnedBots,
  botRingRoute,
  botStatusWords,
  keepMarks,
  listSeats,
  onRail,
  placeName,
  placeRoot,
  railRows,
  railScope,
  railShown,
  readMark,
  fallbackRailUp,
  paneHomeHref,
  plusDoor,
  ringPress,
  ringMarks,
  type RailPlace,
  type RingMarkInput,
} from "./claude-rail-lib";
import { favLike, isBlockedChat } from "./rail-peek-lib";



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

  // Owner, 2026-11-05: "sort order for fav tasks will be new favourites on top".
  it("the newest STAR stands first, whatever the chats' own activity", () => {
    const busy = pulse(task({ key: "busy", happened_at: 900 }), { starred: true, starred_at: 10 });
    const quiet = pulse(task({ key: "quiet", happened_at: 100 }), { starred: true, starred_at: 20 });
    expect(favSeats([busy, quiet], new Map()).map((s) => s.pulse.key)).toEqual(["quiet", "busy"]);
  });

  it("an older server with no stamp: activity order, as before", () => {
    const busy = pulse(task({ key: "busy", happened_at: 900 }), { starred: true });
    const quiet = pulse(task({ key: "quiet", happened_at: 100 }), { starred: true });
    expect(favSeats([quiet, busy], new Map()).map((s) => s.pulse.key)).toEqual(["busy", "quiet"]);
  });
});

// Owner, 2026-11-05: "blocked tasks will also be added on top of favourites
// … even a non-fav task if it is blocked … it is technically a temporary
// favourite without showing it to the user".
describe("favSeats: the blocked at the head", () => {
  it("every blocked, un-archived, UNSTARRED chat with a session stands first, newest activity first; then the starred", () => {
    const waiting = pulse(task({ key: "w", status: "needs_attention", happened_at: 100 }));
    const broke = pulse(task({ key: "b", status: "blocked", happened_at: 200 }));
    const starredBlocked = pulse(task({ key: "sb", status: "blocked", happened_at: 300 }), { starred: true, starred_at: 5 });
    const star = pulse(task({ key: "s", happened_at: 50 }), { starred: true, starred_at: 9 });
    const running = pulse(task({ key: "r", status: "in_progress", happened_at: 400 }));
    const archived = pulse(task({ key: "x", status: "archived", happened_at: 500 }));
    const doorless = pulse(task({ key: "d", status: "blocked", happened_at: 600, session_id: "" }));
    const seats = favSeats([waiting, broke, starredBlocked, star, running, archived, doorless], new Map());
    expect(seats.map((s) => s.pulse.key)).toEqual(["b", "w", "s", "sb"]);
    expect(seats.filter((s) => !s.pulse.starred).map((s) => s.pulse.key)).toEqual(["b", "w"]);
  });
  it("isBlockedChat / favLike: the Blocked lane's two statuses, or a star", () => {
    expect(isBlockedChat({ status: "blocked" })).toBe(true);
    expect(isBlockedChat({ status: "needs_attention" })).toBe(true);
    expect(isBlockedChat({ status: "in_progress" })).toBe(false);
    expect(favLike({ status: "done", starred: true })).toBe(true);
    expect(favLike({ status: "blocked" })).toBe(true);
    expect(favLike({ status: "done" })).toBe(false);
    expect(favLike(undefined)).toBe(false);
  });
  it("the list keeps a blocked chat too — it stands in both sections (2026-11-05)", () => {
    const rows = [
      pulse(task({ key: "a", happened_at: 300 })),
      pulse(task({ key: "blk", status: "blocked", happened_at: 200 })),
      pulse(task({ key: "b", happened_at: 100 })),
    ];
    expect(listSeats(railRows(rows, new Map(), PLACE), true).map((s) => s.pulse.key)).toEqual(["blk", "a", "b"]);
  });
});

describe("listHidden / plusShown / pinnedBots (owner, 2026-11-05)", () => {
  it("the list hides beside the open pane on its home, favourites on, and only then", () => {
    // Since 2026-11-06 the list stands beside the pane's home too.
    expect(listHidden({ paneOpen: true, favsOn: true, paneHome: true })).toBe(false);
    expect(listHidden({ paneOpen: true, favsOn: true, paneHome: false })).toBe(false);
    expect(listHidden({ paneOpen: false, favsOn: true, paneHome: true })).toBe(false);
    expect(listHidden({ paneOpen: true, favsOn: false, paneHome: true })).toBe(false);
  });
  it("the + is a page rail's, never the fallback's", () => {
    expect(plusShown({ favouritesOnly: false })).toBe(true);
    expect(plusShown({ favouritesOnly: true })).toBe(false);
  });
  it("pinned bots: pinned and not hidden, in the pulse's order; an old pulse has none", () => {
    const bots = [
      { id: "a", pinned: true },
      { id: "h", pinned: true, hidden: true },
      { id: "u", pinned: false },
      { id: "old" },
    ];
    expect(pinnedBots(bots).map((b) => b.id)).toEqual(["a"]);
  });
  it("pinned bots in the Bots page's order (2026-11-08): waiting-unread first, then your last message, then newest made", () => {
    const bots = [
      { id: "old", pinned: true, created: 100 },
      { id: "wrote", pinned: true, created: 50, last_user_ts: 900 },
      { id: "wait", pinned: true, status: "waiting", seq: 5, seen_seq: 3, created: 10 },
      { id: "waitRead", pinned: true, status: "waiting", seq: 5, news_seq: 5, seen_seq: 5, created: 20 },
      { id: "wroteEarlier", pinned: true, created: 60, last_user_ts: 500 },
      { id: "unpinned", pinned: false, last_user_ts: 9999 },
    ];
    expect(pinnedBots(bots, { pageOrder: true }).map((b) => b.id)).toEqual(["wait", "wrote", "wroteEarlier", "old", "waitRead"]);
    // Without it, the pulse's order stands.
    expect(pinnedBots(bots).map((b) => b.id)).toEqual(["old", "wrote", "wait", "waitRead", "wroteEarlier"]);
  });
  it("a pinned bot's ring opens its card, on every page, the Bots page too, with the card off, or with no room (2026-11-05)", () => {
    expect(botRingRoute({ peekOn: true, noRoom: false })).toBe("card");
    // The Bots page is no exception any more (2026-11-06): the card there too.
    expect(botRingRoute({ peekOn: false, noRoom: false })).toBe("door");
    expect(botRingRoute({ peekOn: true, noRoom: true })).toBe("door");
  });
  it("the bot card's status words are the page's", () => {
    expect(botStatusWords("waiting")).toBe("waiting for you");
    expect(botStatusWords("running")).toBe("running");
    expect(botStatusWords("idle")).toBe("idle");
    expect(botStatusWords("odd")).toBe("odd");
  });
});

describe("listSeats (the two sections are independent, 2026-11-05)", () => {
  const rows = [
    pulse(task({ key: "a", happened_at: 300 })),
    pulse(task({ key: "fav", happened_at: 200 }), { starred: true }),
    pulse(task({ key: "b", happened_at: 100 })),
  ];
  it("favourites on or off, the list keeps every chat of the place — a starred one stands in both sections", () => {
    const seats = railRows(rows, new Map(), PLACE);
    expect(listSeats(seats, true)).toBe(seats);
    expect(listSeats(seats, false)).toBe(seats);
    expect(seats.map((s) => s.pulse.key)).toEqual(["a", "fav", "b"]);
    expect(favSeats(rows, new Map()).map((s) => s.pulse.key)).toEqual(["fav"]);
  });
  it("a node narrows the list, never the favourites", () => {
    const NODE: RailPlace = { ...PLACE, file: "n.py" };
    const onNode = pulse(task({ key: "n", target: `${DIR}/n.py` }));
    const starredElsewhere = pulse(task({ key: "fav2", target: `${DIR}/other.py` }), { starred: true });
    const all = [onNode, starredElsewhere];
    expect(listSeats(railRows(all, new Map(), NODE), true).map((s) => s.pulse.key)).toEqual(["n"]);
    expect(favSeats(all, new Map()).map((s) => s.pulse.key)).toEqual(["fav2"]);
  });
});

describe("activeRing (one active ring per section)", () => {
  it("the open favourite wins over the section's last press", () => {
    expect(activeRing(["A", "B", "C"], ["B", "C"])).toBe("B");
  });
  it("no open favourite: the last press", () => {
    expect(activeRing(["A", "B", "C"], [null, "C"])).toBe("C");
  });
  it("a want that is not in the section is skipped", () => {
    expect(activeRing(["A", "B"], ["Z", undefined, "A"])).toBe("A");
    expect(activeRing(["A", "B"], ["Z", null])).toBeNull();
    expect(activeRing([], ["A"])).toBeNull();
  });
});

describe("activeRing with a pinned favourite", () => {
  it("a pinned key that is a favourite marks nothing in the list; the list's next want wins", () => {
    // The rail's card pinned over a favourite's hover (a press inside it):
    // the list asks for the pinned key first, but it is not one of the list's.
    const list = ["A", "B"];
    const favs = ["F", "G"];
    expect(activeRing(list, ["F", "B", null])).toBe("B");
    expect(activeRing(list, ["F", null, null])).toBeNull();
    // …and the favourites' section goes by its own wants (open card, then
    // its last press), never by the list's pin.
    expect(activeRing(favs, [null, "G"])).toBe("G");
  });
});

describe("ringMarks (what each ring wears, per section)", () => {
  const base: Omit<RingMarkInput, "key" | "foot" | "isLast" | "sessionHere"> = {
    favsOn: true,
    peekOn: true,
    peekKey: null,
    pinned: false,
    held: false,
    openFavKey: null,
    activeList: null,
    activeFav: null,
  };
  /** Every ring of both sections, as ClaudeRail draws them. */
  function rail(o: {
    list: string[];
    favs: string[];
    lastList?: string;
    lastFav?: string;
    session?: string;
    peekKey?: string | null;
    pinned?: boolean;
    held?: boolean;
    openFavKey?: string | null;
  }) {
    const openFavKey = o.openFavKey ?? null;
    const activeList = activeRing(o.list, [o.pinned ? o.peekKey : null, o.lastList, o.session]);
    const activeFav = activeRing(o.favs, [openFavKey, o.lastFav]);
    const ring = (key: string, foot: boolean) => ({
      key,
      foot,
      ...ringMarks({
        ...base,
        key,
        foot,
        peekKey: o.peekKey ?? null,
        pinned: !!o.pinned,
        held: !!o.held,
        openFavKey,
        sessionHere: key === o.session,
        isLast: key === (foot ? o.lastFav : o.lastList),
        activeList,
        activeFav,
      }),
    });
    return [...o.list.map((k) => ring(k, false)), ...o.favs.map((k) => ring(k, true))];
  }
  const count = (rings: ReturnType<typeof rail>, foot: boolean, cls: string) =>
    rings.filter((r) => r.foot === foot && r.classes.includes(cls)).length;
  const marked = (rings: ReturnType<typeof rail>, foot: boolean) =>
    rings.filter((r) => r.foot === foot && r.classes.some((c) => c === "active" || c === "is-last" || c === "is-open"));

  it("one active ring per section with favourites on", () => {
    const rings = rail({
      list: ["A", "B", "C"],
      favs: ["F", "G"],
      lastList: "B",
      session: "C",
      lastFav: "G",
      openFavKey: "F",
    });
    // The list: its last press wins over the URL's session — one mark.
    expect(marked(rings, false).map((r) => r.key)).toEqual(["B"]);
    expect(count(rings, false, "is-last")).toBe(1);
    expect(count(rings, false, "active")).toBe(0);
    // The favourites: the open card's ring, `is-open`, and no `is-last` beside it.
    expect(marked(rings, true).map((r) => r.key)).toEqual(["F"]);
    expect(count(rings, true, "is-open")).toBe(1);
    expect(count(rings, true, "is-last")).toBe(0);
  });

  it("no favourite open: the section's last press is its one mark", () => {
    const rings = rail({ list: ["A"], favs: ["F", "G"], lastFav: "G", session: "A" });
    expect(marked(rings, true).map((r) => r.key)).toEqual(["G"]);
    expect(marked(rings, false).map((r) => r.key)).toEqual(["A"]);
  });

  it("a favourite card open: no favourite ring opens a hover card; list rings do", () => {
    const rings = rail({ list: ["A"], favs: ["F", "G"], openFavKey: "F" });
    expect(rings.find((r) => r.key === "F")!.hovers).toBe(false);
    expect(rings.find((r) => r.key === "G")!.hovers).toBe(false);
    expect(rings.find((r) => r.key === "A")!.hovers).toBe(true);
  });

  it("no favourite card open: favourite rings hover like list rings", () => {
    const rings = rail({ list: ["A"], favs: ["F", "G"] });
    expect(rings.every((r) => r.hovers)).toBe(true);
  });

  it("the open favourite's chat in the LIST is a list ring like any other (the sections do not see each other, 2026-11-05)", () => {
    const rings = rail({ list: ["A", "S"], favs: ["F"], openFavKey: "S" });
    const s = rings.find((r) => r.key === "S")!;
    expect(s.hovers).toBe(true);
    expect(s.favRing).toBe(false);
    // It is not the foot's ring: no `is-open`, no `is-fav`.
    expect(s.classes).not.toContain("is-open");
    expect(s.classes).not.toContain("is-fav");
  });

  it("a favourite's ring never wears the list's pin; the list's pinned ring does", () => {
    const rings = rail({ list: ["A"], favs: ["F"], peekKey: "F", pinned: true, held: true });
    expect(rings.find((r) => r.key === "F")!.classes).toEqual(["is-fav"]);
    const listPinned = rail({ list: ["A"], favs: ["F"], peekKey: "A", pinned: true });
    expect(listPinned.find((r) => r.key === "A")!.classes).toContain("is-pinned");
  });

  it("a list card pinned, no favourite open: favourite rings hover in window B, untouched by the pin; list rings stay caption-only", () => {
    const rings = rail({ list: ["A", "B"], favs: ["F", "G"], peekKey: "A", pinned: true, held: true });
    const by = (k: string) => rings.find((r) => r.key === k)!;
    // A favourite's ring hovers (its own card) and keeps its caption's rule:
    // the list's pin is not its business.
    for (const k of ["F", "G"]) {
      expect(by(k).hovers).toBe(true);
      expect(by(k).titleOnly).toBe(false);
      expect(by(k).classes).not.toContain("is-pinned");
    }
    // Another list ring: "click only" — its hover is its caption.
    expect(by("B").titleOnly).toBe(true);
    // The pinned ring itself is no caption-only ring.
    expect(by("A").titleOnly).toBe(false);
  });

  it("window B's hover washes only the hovered favourite's ring (`favHoverKey`)", () => {
    const r = ringMarks({ ...base, key: "F", foot: true, sessionHere: false, isLast: false, favHoverKey: "F" });
    expect(r.classes).toContain("peeked");
    const other = ringMarks({ ...base, key: "G", foot: true, sessionHere: false, isLast: false, favHoverKey: "F" });
    expect(other.classes).not.toContain("peeked");
    // The list's peekKey never washes a favourite's ring.
    const viaList = ringMarks({ ...base, key: "F", foot: true, sessionHere: false, isLast: false, peekKey: "F" });
    expect(viaList.classes).not.toContain("peeked");
  });

  it("a list card pinned AND a favourite card open: favourite rings stay caption-only", () => {
    const rings = rail({ list: ["A"], favs: ["F", "G"], peekKey: "A", pinned: true, openFavKey: "F" });
    for (const k of ["F", "G"]) {
      const r = rings.find((x) => x.key === k)!;
      expect(r.hovers).toBe(false);
      expect(r.titleOnly).toBe(true);
    }
  });

  it("favourites off: no ring swaps a pinned card", () => {
    const r = ringMarks({
      ...base,
      favsOn: false,
      key: "F",
      foot: true,
      sessionHere: false,
      isLast: false,
      peekKey: "A",
      pinned: true,
    });
    expect(r.titleOnly).toBe(false);
  });

  it("favourites off: every mark as before, the held accent included", () => {
    const off = (key: string, o: Partial<RingMarkInput>) =>
      ringMarks({ ...base, favsOn: false, key, foot: false, sessionHere: false, isLast: false, ...o }).classes;
    expect(off("A", { sessionHere: true, isLast: true })).toEqual(["active", "is-last"]);
    expect(off("A", { peekKey: "A", pinned: true, held: true })).toEqual(["peeked", "is-pinned", "is-held"]);
  });
});

describe("chipPlace / chipDoor (the where-chip's door)", () => {
  const OTHER = "/home/me/canvases/other";
  it("a row on the rail's place is read against it; one from elsewhere against its own", () => {
    const here = pulse(task({ key: "a", canvas: { env: "unstable", name: "demo", file: "hex.py" } }));
    expect(chipPlace(here, PLACE)).toBe(PLACE);
    const there = pulse(
      task({ key: "b", project: OTHER, target: `${OTHER}/map.py`, canvas: { env: "prod", name: "other", file: "map.py" } }),
    );
    expect(chipPlace(there, PLACE)).toEqual({ kind: "canvas", env: "prod", name: "other", dir: OTHER });
    const folder = pulse(task({ key: "c", canvas: null, project: "/apps/x", target: "/apps/x/main.py" }));
    expect(chipPlace(folder, PLACE)).toEqual({ kind: "folder", dir: "/apps/x", word: "Folder" });
  });

  it("a node on another canvas (or no canvas page) navigates to that canvas with the node selected", () => {
    const at: RailPlace = { kind: "canvas", env: "prod", name: "other", dir: OTHER };
    const door = chipDoor({ kind: "node", name: "map", file: "map.py" }, at, { env: "unstable", name: "demo", file: null })!;
    expect(door.select).toBeUndefined();
    expect(door.href).toBe("/canvases/prod/other?node=map&node_file=map.py&focus=node");
    expect(door.title).toBe("Open map on other");
    expect(chipDoor({ kind: "whole", word: "Canvas" }, at, null)!.href).toBe("/canvases/prod/other");
  });

  it("on this canvas's page the chip selects in place (the node, or the canvas as a whole)", () => {
    const here = { env: "unstable" as const, name: "demo", file: "hex.py" };
    const node = chipDoor({ kind: "node", name: "grid", file: "grid.py" }, PLACE, here)!;
    expect(node.select).toEqual({ udfName: "grid" });
    expect(node.href).toBe("/canvases/unstable/demo?node=grid&node_file=grid.py&focus=node");
    // The whole canvas while a node is selected: the press deselects.
    expect(chipDoor({ kind: "whole", word: "Canvas" }, PLACE, here)!.select).toEqual({ udfName: null });
  });

  it("a chip is a door (fill, link) only when its press does something; else null — plain text", () => {
    const app: RailPlace = { kind: "folder", dir: "/apps/x/", word: "App" };
    const page = placeRoot(app);
    const whole = { kind: "whole", word: "App" } as const;
    // Same folder: nowhere to go (a trailing slash is the same folder).
    expect(chipDoor(whole, app, null, page)).toBeNull();
    expect(chipDoor(whole, { kind: "folder", dir: "/apps/x" }, null, page)).toBeNull();
    // Another folder: a door.
    expect(chipDoor(whole, { kind: "folder", dir: "/apps/y" }, null, page)?.href).toBe("/explorer/view/apps/y");
    // A file under this folder: a door to its page — none on that very page.
    const sub = { kind: "file", name: "src/a.py", file: "/apps/x/src/a.py" } as const;
    expect(chipDoor(sub, app, null, page)?.href).toBe("/explorer/view/apps/x/src/a.py");
    expect(chipDoor(sub, app, null, "file:/apps/x/src/a.py")).toBeNull();
    // A file rail's own file: nowhere; another file: a door.
    const onFile: RailPlace = { kind: "file", file: "/apps/x/b.py" };
    expect(chipDoor({ kind: "whole", word: "File" }, onFile, null, placeRoot(onFile))).toBeNull();
    expect(chipDoor({ kind: "whole", word: "File" }, onFile, null, page)?.href).toBe("/explorer/view/apps/x/b.py");
    // This canvas, no node selected: the whole canvas goes nowhere…
    const here = { env: "unstable" as const, name: "demo", file: null };
    const canvas = { kind: "whole", word: "Canvas" } as const;
    expect(chipDoor(canvas, PLACE, here, placeRoot(PLACE))).toBeNull();
    // …and the same with no in-place select on that page.
    expect(chipDoor(canvas, PLACE, null, placeRoot(PLACE))).toBeNull();
    // A node on this canvas: selects it — unless it already is the selection.
    const hex = { kind: "node", name: "hex", file: "hex.py" } as const;
    expect(chipDoor(hex, PLACE, here, placeRoot(PLACE))?.select).toEqual({ udfName: "hex" });
    expect(chipDoor(hex, PLACE, { ...here, file: "hex.py" }, placeRoot(PLACE))).toBeNull();
    // Another canvas: a door, whole or node.
    const other: RailPlace = { kind: "canvas", env: "prod", name: "other", dir: OTHER };
    expect(chipDoor(canvas, other, here, placeRoot(PLACE))?.href).toBe("/canvases/prod/other");
    expect(chipDoor(hex, other, here, placeRoot(PLACE))?.select).toBeUndefined();
    // A folder chip seen from a canvas page: a door.
    expect(chipDoor(whole, app, here, placeRoot(PLACE))?.isDir).toBe(true);
  });

  it("chipPath: the plain chip's title is the full place, no verb", () => {
    const app: RailPlace = { kind: "folder", dir: "/apps/x/", word: "App" };
    expect(chipPath({ kind: "whole", word: "App" }, app)).toBe("/apps/x");
    expect(chipPath({ kind: "file", name: "src/a.py", file: "/apps/x/src/a.py" }, app)).toBe("/apps/x/src/a.py");
    expect(chipPath({ kind: "whole", word: "File" }, { kind: "file", file: "/apps/x/b.py" })).toBe("/apps/x/b.py");
    expect(chipPath({ kind: "whole", word: "Canvas" }, PLACE)).toBe("demo");
    expect(chipPath({ kind: "node", name: "hex", file: "hex.py" }, PLACE)).toBe("hex on demo");
  });

  it("favourites off: never null — every chip stays the door it was on main, on its own place too", () => {
    const app: RailPlace = { kind: "folder", dir: "/apps/x/", word: "App" };
    const whole = { kind: "whole", word: "App" } as const;
    // A folder chip on its own page is a plain door (main opened it again).
    expect(chipDoor(whole, app, null, placeRoot(app), false)).toEqual({
      href: "/explorer/view/apps/x",
      isDir: true,
      title: "Open /apps/x",
    });
    expect(chipDoor(whole, { kind: "folder", dir: "/apps/y" }, null, placeRoot(app), false)?.current).toBeUndefined();
    const sub = { kind: "file", name: "src/a.py", file: "/apps/x/src/a.py" } as const;
    expect(chipDoor(sub, app, null, "file:/apps/x/src/a.py", false)).toEqual({
      href: "/explorer/view/apps/x/src/a.py",
      title: "Open /apps/x/src/a.py",
    });
    const onFile: RailPlace = { kind: "file", file: "/apps/x/b.py" };
    expect(chipDoor({ kind: "whole", word: "File" }, onFile, null, placeRoot(onFile), false)).toEqual({
      href: "/explorer/view/apps/x/b.py",
      title: "Open /apps/x/b.py",
    });
    const canvas = { kind: "whole", word: "Canvas" } as const;
    // No selection seat on the page: a plain door, as on main.
    expect(chipDoor(canvas, PLACE, null, placeRoot(PLACE), false)).toEqual({
      href: "/canvases/unstable/demo",
      title: "Open the canvas demo",
    });
    // This canvas, that node already selected: the only `current` chip, main's title.
    const here = { env: "unstable" as const, name: "demo", file: "hex.py" };
    const hex = { kind: "node", name: "hex", file: "hex.py" } as const;
    expect(chipDoor(hex, PLACE, here, placeRoot(PLACE), false)).toMatchObject({
      select: { udfName: "hex" },
      current: true,
      title: "On hex, selected",
    });
    expect(chipDoor(canvas, PLACE, { ...here, file: null }, placeRoot(PLACE), false)).toMatchObject({
      select: { udfName: null },
      current: true,
      title: "On the canvas demo",
    });
    // Another node / the whole canvas while a node is selected: select in place.
    expect(chipDoor(canvas, PLACE, here, placeRoot(PLACE), false)?.current).toBeUndefined();
    expect(chipDoor(hex, PLACE, { ...here, file: "grid.py" }, placeRoot(PLACE), false)?.current).toBeUndefined();
  });

  it("chipLabel: a folder or a file by its own name, never the bare word; a page's own word stays", () => {
    const folder: RailPlace = { kind: "folder", dir: "/Users/me/Fused/app/insta-edit-pro/", word: "Folder" };
    expect(chipLabel({ kind: "whole", word: "Folder" }, folder)).toBe("insta-edit-pro");
    expect(chipLabel({ kind: "whole", word: "Folder" }, { kind: "folder", dir: "/x/y" })).toBe("y");
    expect(chipLabel({ kind: "file", name: "src/a.py", file: "/x/y/src/a.py" }, folder)).toBe("src/a.py");
    expect(chipLabel({ kind: "whole", word: "File" }, { kind: "file", file: "/x/y/notes.md" })).toBe("notes.md");
    expect(chipLabel({ kind: "whole", word: "App" }, { kind: "folder", dir: "/x/y", word: "App" })).toBe("App");
    expect(chipLabel({ kind: "whole", word: "Canvas" }, PLACE)).toBe("Canvas");
    expect(chipLabel({ kind: "node", name: "hex", file: "hex.py" }, PLACE)).toBe("hex");
    // …and the door under the name is the folder's page, its path the title.
    const door = chipDoor({ kind: "whole", word: "Folder" }, folder, null)!;
    expect(door.href).toBe("/explorer/view/Users/me/Fused/app/insta-edit-pro");
    expect(door.isDir).toBe(true);
    expect(door.title).toBe("Open /Users/me/Fused/app/insta-edit-pro");
  });

  it("a folder chip opens the explorer folder page; a file chip its file page", () => {
    const app: RailPlace = { kind: "folder", dir: "/apps/x/", word: "App" };
    const whole = chipDoor({ kind: "whole", word: "App" }, app, null)!;
    expect(whole.href).toBe("/explorer/view/apps/x");
    expect(whole.isDir).toBe(true);
    expect(whole.title).toBe("Open /apps/x");
    const file = chipDoor({ kind: "file", name: "src/a.py", file: "/apps/x/src/a.py" }, app, null)!;
    expect(file.href).toBe("/explorer/view/apps/x/src/a.py");
    expect(file.isDir).toBeUndefined();
    const onFile = chipDoor({ kind: "whole", word: "File" }, { kind: "file", file: "/apps/x/b.py" }, null)!;
    expect(onFile.href).toBe("/explorer/view/apps/x/b.py");
  });
});


// ---- WHETHER THE RAIL STANDS (owner, 2026-11-05) ---------------------------
// "We show the rail no matter what now … even if the sidebar is expanded or
// not. The only reason we don't show the rail is if there are no tasks in
// favourites AND no items to be shown in the rail."
describe("railShown", () => {
  const at = (list: number, favs: number, paneOpen: boolean, always: boolean) =>
    railShown({ list, favs, paneOpen, always });

  it("the pane shut: always — the rail is the way back in, empty or not, flag on or off", () => {
    for (const always of [true, false]) {
      expect(at(0, 0, false, always)).toBe(true);
      expect(at(3, 0, false, always)).toBe(true);
      expect(at(0, 2, false, always)).toBe(true);
    }
  });

  it("the pane open, favourites on: a list chat or a favourite is enough", () => {
    expect(at(1, 0, true, true)).toBe(true);
    expect(at(0, 1, true, true)).toBe(true);
    expect(at(4, 2, true, true)).toBe(true);
  });

  it("the pane open, favourites on: nothing to draw, no rail", () => {
    expect(at(0, 0, true, true)).toBe(false);
  });

  it("the pane open, favourites off: never — the rail of 2026-11-04", () => {
    expect(at(5, 0, true, false)).toBe(false);
    expect(at(0, 3, true, false)).toBe(false);
    expect(at(0, 0, true, false)).toBe(false);
  });
});

describe("railShown, the shell's fallback (favouritesOnly)", () => {
  it("favourites on: shown for a favourite, a blocked chat or a pinned bot — never empty (no +, 2026-11-05)", () => {
    expect(railShown({ list: 0, favs: 1, paneOpen: false, always: true, favouritesOnly: true })).toBe(true);
    expect(railShown({ list: 0, favs: 0, bots: 1, paneOpen: false, always: true, favouritesOnly: true })).toBe(true);
    expect(railShown({ list: 0, favs: 0, blocked: 2, paneOpen: false, always: true, favouritesOnly: true })).toBe(true);
    expect(railShown({ list: 0, favs: 0, paneOpen: false, always: true, favouritesOnly: true })).toBe(false);
  });
  it("the pane open on a page rail: a pinned bot or a blocked chat is enough to stand", () => {
    expect(railShown({ list: 0, favs: 0, bots: 1, paneOpen: true, always: true })).toBe(true);
    expect(railShown({ list: 0, favs: 0, blocked: 1, paneOpen: true, always: true })).toBe(true);
  });
  it("favourites off: never", () => {
    expect(railShown({ list: 0, favs: 2, paneOpen: false, always: false, favouritesOnly: true })).toBe(false);
  });
});

// ---- THE SHELL'S FALLBACK RAIL (owner, 2026-11-05) -------------------------
describe("fallbackRailUp", () => {
  it("favourites on and no page rail mounted: the fallback is up", () => {
    expect(fallbackRailUp({ favsOn: true, pageRails: 0 })).toBe(true);
  });
  it("a page rail mounted: never both", () => {
    expect(fallbackRailUp({ favsOn: true, pageRails: 1 })).toBe(false);
    expect(fallbackRailUp({ favsOn: true, pageRails: 2 })).toBe(false);
  });
  it("favourites off: no fallback", () => {
    expect(fallbackRailUp({ favsOn: false, pageRails: 0 })).toBe(false);
  });
  it("the rail's nav doors (rail flag) put it up with favourites off, never beside a page rail", () => {
    expect(fallbackRailUp({ favsOn: false, pageRails: 0, navDoors: true })).toBe(true);
    expect(fallbackRailUp({ favsOn: false, pageRails: 1, navDoors: true })).toBe(false);
  });
});

// ---- THE + AND A RING'S PRESS BY THE PANE (owner, 2026-11-05) --------------
describe("plusDoor", () => {
  it("pane open, favourites on, a page that can show its home: the pane", () => {
    expect(plusDoor({ paneOpen: true, favsOn: true, paneHome: true })).toBe("pane");
  });
  it("pane shut: the New task card, as today", () => {
    expect(plusDoor({ paneOpen: false, favsOn: true, paneHome: true })).toBe("modal");
  });
  it("no home to show, or favourites off: the card", () => {
    expect(plusDoor({ paneOpen: true, favsOn: true, paneHome: false })).toBe("modal");
    expect(plusDoor({ paneOpen: true, favsOn: false, paneHome: true })).toBe("modal");
  });
});

describe("ringPress (a list ring's plain press)", () => {
  it("pane open, favourites on: the chat opens in the pane — no card", () => {
    expect(ringPress({ paneOpen: true, favsOn: true, peekOn: true, noRoom: false })).toBe("pane");
    expect(ringPress({ paneOpen: true, favsOn: true, peekOn: true, noRoom: true })).toBe("pane");
  });
  it("pane shut: the card pins, as today", () => {
    expect(ringPress({ paneOpen: false, favsOn: true, peekOn: true, noRoom: false })).toBe("card");
  });
  it("no room for a card, or the card off: the chat's door", () => {
    expect(ringPress({ paneOpen: false, favsOn: true, peekOn: true, noRoom: true })).toBe("door");
    expect(ringPress({ paneOpen: false, favsOn: false, peekOn: false, noRoom: false })).toBe("door");
  });
});



describe("paneHomeHref", () => {
  it("drops the conversation's params, keeps the rest", () => {
    expect(paneHomeHref("/apps/x?_tab=overview&session_id=s1&backend=claude#h")).toBe("/apps/x?_tab=overview#h");
    expect(paneHomeHref("http://127.0.0.1:1880/explorer/view/a?queued=q1")).toBe("/explorer/view/a");
  });
  it("no conversation named: home already (null)", () => {
    expect(paneHomeHref("/apps/x?_tab=overview")).toBeNull();
  });
  it("a streaming turn's `run` is a conversation too, and goes with it (2026-11-08)", () => {
    // The + beside a running task's pane dropped `session_id` but kept `run`, and
    // the remounted "home" resumed that very run.
    expect(paneHomeHref("/apps/x?session_id=s1&run=r1&_tab=overview")).toBe("/apps/x?_tab=overview");
    expect(paneHomeHref("/apps/x?run=r1")).toBe("/apps/x");
  });
});

describe("ringMarks with the Claude pane open", () => {
  const o = { favsOn: true, peekOn: true, peekKey: null, pinned: false, held: false, openFavKey: null, activeList: null, activeFav: null, sessionHere: false, isLast: false };
  it("a list ring shows the title only (no window-A hover); a favourite ring still hovers", () => {
    const list = ringMarks({ ...o, key: "a", foot: false, paneOpen: true });
    expect(list.hovers).toBe(false);
    expect(list.titleOnly).toBe(true);
    expect(ringMarks({ ...o, key: "a", foot: true, paneOpen: true }).hovers).toBe(true);
    // A favourite whose chat the pane shows still hovers (2026-11-05: the
    // chat may stand in the pane and in window B at once).
    const held = ringMarks({ ...o, key: "a", foot: true, paneOpen: true, chatElsewhere: true });
    expect(held.hovers).toBe(true);
    expect(held.titleOnly).toBe(false);
    expect(ringMarks({ ...o, key: "a", foot: false, paneOpen: false }).hovers).toBe(true);
  });
});

describe("ringMarks: a section of one ring wears no last mark (2026-11-06)", () => {
  const base = {
    favsOn: true, peekOn: true, peekKey: null, pinned: false, held: false, openFavKey: null,
    sessionHere: false, isLast: true, activeList: "a", activeFav: "a",
  };
  it("favourites on: one ring, no mark; two rings, the mark", () => {
    expect(ringMarks({ ...base, key: "a", foot: false, sectionSize: 1 }).classes).not.toContain("is-last");
    expect(ringMarks({ ...base, key: "a", foot: false, sectionSize: 2 }).classes).toContain("is-last");
    expect(ringMarks({ ...base, key: "a", foot: true, sectionSize: 1 }).classes).not.toContain("is-last");
  });
  it("favourites off: as before, the mark whatever the count", () => {
    expect(ringMarks({ ...base, favsOn: false, key: "a", foot: false, sectionSize: 1 }).classes).toContain("is-last");
  });
});

describe("placeTags (the place as one or two tags, 2026-11-06)", () => {
  const canvas = { kind: "canvas" as const, env: "prod" as const, name: "HLS_Data", dir: "/c/HLS_Data" };
  const folder = { kind: "folder" as const, dir: "/a/app/" };
  it("canvas › Canvas: the canvas's page, then the fit ask — in place on its own canvas", () => {
    const away = placeTags({ kind: "whole", word: "Canvas" }, canvas, { pageRoot: "folder:/x", here: false, sessionId: "s1" });
    expect(away.map((t) => t.label)).toEqual(["HLS_Data", "Canvas"]);
    expect(away[0].door?.href).toBe("/canvases/prod/HLS_Data?session_id=s1&focus=view");
    expect(away[1].door?.href).toBe("/canvases/prod/HLS_Data?session_id=s1&focus=canvas");
    expect(away[1].door?.select).toBeUndefined();
    const own = placeTags({ kind: "whole", word: "Canvas" }, canvas, { pageRoot: "canvas:prod:HLS_Data", here: true });
    expect(own[0].door).toBeNull();
    expect(own[1].door?.select).toEqual({ udfName: null });
  });
  it("canvas › node: the node's chat on the canvas page, the select ask — in place on its own canvas", () => {
    const away = placeTags({ kind: "node", name: "hex", file: "hex.py" }, canvas, { pageRoot: null, here: false, sessionId: "s1" });
    expect(away[0].door?.href).toBe("/canvases/prod/HLS_Data?session_id=s1&focus=view");
    expect(away[1].label).toBe("hex");
    expect(away[1].door?.href).toBe("/canvases/prod/HLS_Data?node=hex&node_file=hex.py&session_id=s1&focus=node");
    const own = placeTags({ kind: "node", name: "hex", file: "hex.py" }, canvas, { pageRoot: "canvas:prod:HLS_Data", here: true });
    expect(own[0].door).toBeNull();
    expect(own[1].door?.select).toEqual({ udfName: "hex" });
  });
  it("folder › file, folder alone, file alone: the explorer's pages, plain where the page stands", () => {
    const ff = placeTags({ kind: "file", name: "index.html", file: "/a/app/index.html" }, folder, { pageRoot: "folder:/a/app", here: false });
    expect(ff.map((t) => t.label)).toEqual(["app", "index.html"]);
    expect(ff[0].door).toBeNull();
    expect(ff[1].door?.href).toBe("/explorer/view/a/app/index.html");
    const f = placeTags({ kind: "whole", word: "Folder" }, folder, { pageRoot: null, here: false });
    expect(f).toHaveLength(1);
    expect(f[0].door?.isDir).toBe(true);
    const file = placeTags({ kind: "whole", word: "File" }, { kind: "file", file: "/a/app/x.py" }, { pageRoot: "file:/a/app/x.py", here: false });
    expect(file).toHaveLength(1);
    expect(file[0].label).toBe("x.py");
    expect(file[0].door).toBeNull();
  });
});
