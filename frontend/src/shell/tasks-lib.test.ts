// The Tasks page's rules, without a DOM: the accordion's Show-more state, the
// per-message unread bookkeeping, the board's drag legality, filtering, and the
// one ordering promise the client makes (it keeps the server's).
import { describe, expect, it } from "bun:test";
import type { Task, TaskMessage } from "@platform/lib/api";
import {
  EMPTY_FILTERS,
  MESSAGE_ANCHOR_PARAM,
  PREVIEW_MESSAGES,
  basename,
  canCancel,
  cancelIntent,
  dayLabel,
  dropLanes,
  filterTasks,
  firstLine,
  groupByColumn,
  isDraggable,
  isUnread,
  markRead,
  messageHref,
  messageTime,
  messageTone,
  openMessageHref,
  projectOptions,
  readKey,
  taskColumn,
  taskHref,
  taskUnread,
  threadView,
  tildePath,
  toggleExpanded,
  triageStatus,
} from "./tasks-lib";

// 2026-08-16 is a Sunday; 2026-08-10 a Monday.
const NOW = Date.parse("2026-08-16T12:00:00");

function msg(over: Partial<TaskMessage> = {}): TaskMessage {
  return {
    message_id: "MSG-001",
    kind: "scheduled",
    body: "pull today's news",
    at: Math.floor(Date.parse("2026-08-16T09:00:00") / 1000),
    state: "sent",
    unread: false,
    entry_id: "e1",
    template_id: "",
    turn: "done",
    anchor: "uuid-1",
    ...over,
  };
}

/** A task with `n` messages, MSG-n newest first, as the server sends them. */
function task(over: Partial<Task> = {}, n = 1): Task {
  const messages: TaskMessage[] = [];
  for (let i = n; i >= 1; i--) {
    messages.push(msg({ message_id: `MSG-${String(i).padStart(3, "0")}` }));
  }
  return {
    key: "sess-1",
    task_id: "TASK-002",
    project: "/Users/me/Desktop/fused",
    target: "/Users/me/Desktop/fused",
    session_id: "sess-1",
    title: "Pull today's news",
    title_source: "ai",
    description: "",
    status: "done",
    failed: false,
    live: false,
    unread: 0,
    last_active: Math.floor(NOW / 1000),
    message_count: n,
    // The server only ever sends the three newest on the listing path.
    messages: messages.slice(0, PREVIEW_MESSAGES),
    ...over,
  };
}

// ---- the accordion: 1 / 3 / 12 messages --------------------------------------

describe("threadView", () => {
  it("shows one sub-item and no Show more for a one-message task", () => {
    const view = threadView(task({}, 1));
    expect(view.messages.length).toBe(1);
    expect(view.more).toBe(false);
    expect(view.hidden).toBe(0);
  });

  it("shows three and no Show more at exactly three", () => {
    const view = threadView(task({}, 3));
    expect(view.messages.map((m) => m.message_id)).toEqual([
      "MSG-003", "MSG-002", "MSG-001",
    ]);
    expect(view.more).toBe(false);
  });

  it("offers Show more at twelve, and says how many are hidden", () => {
    const view = threadView(task({}, 12));
    expect(view.messages.length).toBe(PREVIEW_MESSAGES);
    expect(view.more).toBe(true);
    expect(view.hidden).toBe(9);
  });

  it("REPLACES the preview with the loaded thread rather than appending", () => {
    const t = task({}, 12);
    const full: TaskMessage[] = [];
    for (let i = 12; i >= 1; i--) full.push(msg({ message_id: `MSG-${i}` }));
    const view = threadView(t, full);
    expect(view.messages.length).toBe(12);
    expect(new Set(view.messages.map((m) => m.message_id)).size).toBe(12);
    expect(view.more).toBe(false);
  });

  it("trusts message_count, not the preview length, for 'is there more?'", () => {
    // A server that sent fewer than three but claims more still gets a button.
    expect(threadView({ ...task({}, 1), message_count: 4 }).more).toBe(true);
    // ...and one that claims fewer than it sent never goes negative.
    expect(threadView({ ...task({}, 3), message_count: 1 }).hidden).toBe(0);
  });
});

describe("toggleExpanded", () => {
  it("starts collapsed and flips one key at a time, without mutating", () => {
    const a = new Set<string>();
    const b = toggleExpanded(a, "t1");
    expect(a.has("t1")).toBe(false); // untouched
    expect(b.has("t1")).toBe(true);
    const c = toggleExpanded(b, "t2");
    expect([...c].sort()).toEqual(["t1", "t2"]);
    expect(toggleExpanded(c, "t1").has("t1")).toBe(false);
    expect(toggleExpanded(c, "t1").has("t2")).toBe(true);
  });
});

// ---- unread ------------------------------------------------------------------

describe("unread", () => {
  const three = (): Task => ({
    ...task({ unread: 3 }, 3),
    messages: [
      msg({ message_id: "MSG-003", unread: true }),
      msg({ message_id: "MSG-002", unread: true }),
      msg({ message_id: "MSG-001", unread: true }),
    ],
  });

  it("counts what the server said before anything is clicked", () => {
    expect(taskUnread(three(), new Set())).toBe(3);
  });

  it("marking one read leaves the older ones unread", () => {
    const t = three();
    const read = markRead(new Set<string>(), t.key, "MSG-003");
    expect(isUnread(t.key, t.messages[0], read)).toBe(false);
    expect(isUnread(t.key, t.messages[1], read)).toBe(true);
    expect(isUnread(t.key, t.messages[2], read)).toBe(true);
    expect(taskUnread(t, read)).toBe(2);
  });

  it("never double-counts a message that was already read", () => {
    const t = three();
    let read = markRead(new Set<string>(), t.key, "MSG-003");
    read = markRead(read, t.key, "MSG-003");
    expect(taskUnread(t, read)).toBe(2);
    // A message the server already called read costs nothing when clicked.
    const seen = { ...t, messages: t.messages.map((m) => ({ ...m, unread: false })) };
    expect(taskUnread(seen, markRead(read, t.key, "MSG-002"))).toBe(3);
  });

  it("does not go negative when the poll has already caught up", () => {
    const t = { ...three(), unread: 0 };
    expect(taskUnread(t, markRead(new Set<string>(), t.key, "MSG-003"))).toBe(0);
  });

  it("keys reads per task — MSG-001 exists in every thread", () => {
    expect(readKey("a", "MSG-001")).not.toBe(readKey("b", "MSG-001"));
    const t = three();
    const other = markRead(new Set<string>(), "some-other-task", "MSG-003");
    expect(taskUnread(t, other)).toBe(3);
  });

  it("discounts against the LOADED thread once Show more has run", () => {
    const t = { ...three(), unread: 12, message_count: 12 };
    const full = [
      msg({ message_id: "MSG-012", unread: true }),
      msg({ message_id: "MSG-011", unread: true }),
    ];
    expect(taskUnread(t, markRead(new Set<string>(), t.key, "MSG-011"), full)).toBe(11);
  });
});

// ---- per-message state -------------------------------------------------------

describe("messageTone", () => {
  it("never paints a failed or missed message as a clean run", () => {
    expect(messageTone(msg({ state: "error" }))).toMatchObject({
      column: "done", failed: true,
    });
    expect(messageTone(msg({ state: "missed", template_id: "" }))).toMatchObject({
      column: "done", failed: true,
    });
    expect(messageTone(msg({ state: "sent", turn: "unknown" }))).toMatchObject({
      column: "done", failed: true,
    });
  });

  it("files a skipped occurrence away rather than flagging it", () => {
    // The loop's own skip-not-catch-up verdict on a recurring message is
    // routine — the next run is already coming.
    expect(messageTone(msg({ state: "missed", template_id: "t1" }))).toMatchObject({
      column: "archived", failed: false, label: "Skipped",
    });
    expect(messageTone(msg({ state: "skipped" })).failed).toBe(false);
    expect(messageTone(msg({ state: "cancelled" })).column).toBe("archived");
  });

  it("reads a sent message by its turn, not by the send", () => {
    expect(messageTone(msg({ state: "sent", turn: "done" }))).toMatchObject({
      column: "done", failed: false,
    });
    // Sent, no verdict yet: still running. The ONLY value that means that.
    expect(messageTone(msg({ state: "sent", turn: "" })).column).toBe("in_progress");
    expect(messageTone(msg({ state: "pending" })).column).toBe("upcoming");
    expect(messageTone(msg({ state: "sending" })).column).toBe("in_progress");
  });

  it("reads an idle turn as ran, never as still running", () => {
    // `idle` = the turn ended and reported, nothing is live. It is a SECOND
    // word for the same outcome as `done`, and the calendar once painted it
    // "Running" because only `done` was named here.
    expect(messageTone(msg({ state: "sent", turn: "idle" }))).toMatchObject({
      column: "done", failed: false, label: "Ran",
    });
  });

  it("does not call an unheard-of turn word in-flight", () => {
    // `turn` is written once, when the turn ENDS, so a word this build does not
    // know is a turn that finished — not one still going. Defaulting the other
    // way freezes the row on "Running…" forever.
    const future = msg({ state: "sent", turn: "settled" as TaskMessage["turn"] });
    expect(messageTone(future)).toMatchObject({ column: "done", failed: false });
  });
});

describe("taskColumn", () => {
  it("renders the server's status and keeps an unknown one off the floor", () => {
    expect(taskColumn(task({ status: "upcoming" }))).toBe("upcoming");
    expect(taskColumn(task({ status: "archived" }))).toBe("archived");
    expect(taskColumn({ ...task(), status: "weird" as Task["status"] })).toBe("done");
  });
});

// ---- drag --------------------------------------------------------------------

describe("dropLanes", () => {
  it("a task with no session has nothing to triage, so it cannot lift", () => {
    const pending = task({ key: "pending:e1", session_id: "", status: "upcoming" });
    expect(dropLanes(pending)).toEqual([]);
    expect(isDraggable(pending)).toBe(false);
  });

  it("offers the other two triage lanes, never the one it is already in", () => {
    expect(dropLanes(task({ status: "done" }))).toEqual(["in_progress", "archived"]);
    expect(dropLanes(task({ status: "in_progress" }))).toEqual(["done", "archived"]);
    // Upcoming is not a triage value, so a task there may move to all three.
    expect(dropLanes(task({ status: "upcoming" }))).toEqual([
      "in_progress", "done", "archived",
    ]);
    expect(isDraggable(task({ status: "done" }))).toBe(true);
  });

  it("refuses to send a lane setSessionTriage does not accept", () => {
    expect(triageStatus("upcoming")).toBe(null);
    expect(triageStatus("done")).toBe("done");
  });
});

// ---- filters -----------------------------------------------------------------

describe("filters", () => {
  const tasks = [
    task({ key: "a", task_id: "TASK-003", title: "Pull today's news",
           project: "/Users/me/news", status: "upcoming" }),
    task({ key: "b", task_id: "TASK-001", title: "Review PRs",
           project: "/Users/me/code", status: "done" }),
    task({ key: "c", task_id: "TASK-002", title: "Tidy downloads",
           project: "/Users/me/news", status: "archived" }),
  ];

  it("filters by status", () => {
    const out = filterTasks(tasks, { ...EMPTY_FILTERS, statuses: ["done", "archived"] });
    expect(out.map((t) => t.key)).toEqual(["b", "c"]);
  });

  it("filters by project", () => {
    const out = filterTasks(tasks, { ...EMPTY_FILTERS, projects: ["/Users/me/news"] });
    expect(out.map((t) => t.key)).toEqual(["a", "c"]);
  });

  it("ands status with project", () => {
    const out = filterTasks(tasks, {
      ...EMPTY_FILTERS, projects: ["/Users/me/news"], statuses: ["archived"],
    });
    expect(out.map((t) => t.key)).toEqual(["c"]);
  });

  it("searches title, task id, path, bodies and the unprinted session id", () => {
    const hit = (q: string) =>
      filterTasks(tasks, { ...EMPTY_FILTERS, search: q }).map((t) => t.key);
    expect(hit("Review")).toEqual(["b"]); // title
    expect(hit("/Users/me/code")).toEqual(["b"]); // project path
    expect(hit("task-001")).toEqual(["b"]); // the printed id, case-insensitively
    expect(hit("pull today")).toEqual(["a", "b", "c"]); // every message body
    expect(hit("  Review  ")).toEqual(["b"]); // the query is trimmed
    expect(hit("sess-1")).toEqual(["a", "b", "c"]);
    expect(hit("nothing here")).toEqual([]);
  });

  it("keeps the server's order — filtering never re-sorts", () => {
    const out = filterTasks(tasks, EMPTY_FILTERS);
    expect(out.map((t) => t.task_id)).toEqual(["TASK-003", "TASK-001", "TASK-002"]);
    expect(
      filterTasks(tasks, { ...EMPTY_FILTERS, projects: ["/Users/me/news"] })
        .map((t) => t.task_id),
    ).toEqual(["TASK-003", "TASK-002"]);
  });

  it("offers every project that has a task, once, sorted", () => {
    expect(projectOptions(tasks)).toEqual(["/Users/me/code", "/Users/me/news"]);
    expect(projectOptions([])).toEqual([]);
  });
});

describe("groupByColumn", () => {
  it("gives every lane a list and keeps the server's order inside it", () => {
    const map = groupByColumn([
      task({ key: "a", status: "done" }),
      task({ key: "b", status: "upcoming" }),
      task({ key: "c", status: "done" }),
    ]);
    expect([...map.keys()]).toEqual(["upcoming", "in_progress", "done", "archived"]);
    expect(map.get("done")!.map((t) => t.key)).toEqual(["a", "c"]);
    expect(map.get("in_progress")).toEqual([]);
  });
});

// ---- links -------------------------------------------------------------------

describe("hrefs", () => {
  it("extends the explorer url with the message's transcript anchor", () => {
    const t = task();
    const base = taskHref(t)!;
    expect(base).toBe(
      "/explorer/view/Users/me/Desktop/fused?_side=claude&session_id=sess-1",
    );
    expect(messageHref(t, msg({ anchor: "rec-9" }))).toBe(`${base}&msg=rec-9`);
  });

  it("falls back to the top of the thread when there is no anchor", () => {
    const t = task();
    expect(messageHref(t, msg({ anchor: "" }))).toBe(taskHref(t));
  });

  it("has nowhere to go before the first run mints a session", () => {
    const t = task({ session_id: "" });
    expect(taskHref(t)).toBe(null);
    expect(messageHref(t, msg())).toBe(null);
  });

  // openMessageHref is what every view that LISTS messages asks, and the
  // calendar is why it exists: its popover shows projected occurrences beside
  // real messages, and the two must not link the same way.
  it("lands a calendar click on the turn, exactly as the list's does", () => {
    const t = task();
    const m = msg({ anchor: "rec-9" });
    expect(openMessageHref(t, m)).toBe(messageHref(t, m));
    expect(openMessageHref(t, m)).toContain("&msg=rec-9");
  });

  it("builds no msg param for a message with no anchor", () => {
    const t = task();
    const to = openMessageHref(t, msg({ anchor: "" }));
    expect(to).toBe(taskHref(t));
    expect(to).not.toContain("msg=");
  });

  it("offers no link at all before the first run mints a session", () => {
    expect(openMessageHref(task({ session_id: "" }), msg({ anchor: "rec-9" }))).toBe(null);
  });

  it("offers no link on a projected occurrence, however real it looks", () => {
    const t = task();
    // A ghost is cron arithmetic: the session exists, the run does not. Even
    // handed an anchor it must not produce a url — there is no turn to land on.
    expect(openMessageHref(t, msg({ message_id: "GHOST-1" }))).toBe(null);
    expect(openMessageHref(t, msg({ message_id: "GHOST-1", anchor: "rec-9" }))).toBe(null);
    // The real message beside it in the same popover still opens.
    expect(openMessageHref(t, msg({ message_id: "MSG-004", anchor: "rec-9" })))
      .toContain("&msg=rec-9");
  });

  it("names the anchor param once, and escapes what it carries", () => {
    // The claude template reads this exact key (`fused.params.get("msg")`), and
    // the value is a transcript uuid off disk — untrusted enough that it has to
    // survive being a query value rather than terminate it.
    expect(MESSAGE_ANCHOR_PARAM).toBe("msg");
    const t = task();
    expect(messageHref(t, msg({ anchor: "a&b=c" }))).toBe(
      `${taskHref(t)}&msg=a%26b%3Dc`,
    );
  });
});

// ---- cancelling ---------------------------------------------------------------

describe("cancelIntent", () => {
  it("offers nothing on a message that has already gone out", () => {
    for (const state of ["sending", "sent", "error", "missed", "cancelled", "skipped"] as const)
      expect(cancelIntent(msg({ state }))).toBe(null);
  });

  it("offers nothing on a chat message — it was delivered when it was typed", () => {
    expect(cancelIntent(msg({ kind: "chat", state: "pending", entry_id: "" }))).toBe(null);
  });

  it("cancels a one-off pending message outright", () => {
    const intent = cancelIntent(msg({ state: "pending", entry_id: "e7" }))!;
    expect(intent.id).toBe("e7");
    expect(intent.scope).toBe("message");
    expect(intent.label).toBe("Cancel");
  });

  it("skips ONE run of a repeat, and says so", () => {
    // The whole point: the id sent is the occurrence's own, never the template
    // it came from — the server reads a template cancel as "stop every further
    // run", so resolving upward the way Edit does would delete a schedule the
    // user meant to skip one run of.
    const intent = cancelIntent(
      msg({ state: "pending", entry_id: "occ-3", template_id: "tmpl-1" }),
    )!;
    expect(intent.id).toBe("occ-3");
    expect(intent.id).not.toBe("tmpl-1");
    expect(intent.scope).toBe("occurrence");
    expect(intent.label).toBe("Skip this run");
    // The consequence is spelled out where a person can read it before clicking.
    expect(intent.title).toContain("keeps going");
  });

  it("canCancel agrees with cancelIntent", () => {
    expect(canCancel(msg({ state: "pending" }))).toBe(true);
    expect(canCancel(msg({ state: "sent" }))).toBe(false);
  });
});

// ---- time --------------------------------------------------------------------

describe("messageTime", () => {
  const at = (iso: string) => Math.floor(Date.parse(iso) / 1000);

  it("speaks the spec's own wording", () => {
    expect(messageTime(at("2026-08-16T09:00:00"), NOW)).toBe("09:00 today");
    expect(messageTime(at("2026-08-15T09:00:00"), NOW)).toBe("09:00 yesterday");
    expect(messageTime(at("2026-08-10T09:00:00"), NOW)).toBe("09:00 Monday");
    expect(messageTime(at("2026-08-17T09:00:00"), NOW)).toBe("09:00 tomorrow");
  });

  it("counts calendar days, not elapsed hours", () => {
    // 23:59 yesterday is one minute before 00:01 today, and still yesterday.
    const near = Date.parse("2026-08-16T00:01:00");
    expect(messageTime(at("2026-08-15T23:59:00"), near)).toBe("23:59 yesterday");
  });

  it("falls back to a date beyond the surrounding week", () => {
    expect(messageTime(at("2026-08-01T14:05:00"), NOW)).toBe("14:05 1 Aug");
    expect(messageTime(at("2025-12-24T14:05:00"), NOW)).toBe("14:05 24 Dec 2025");
  });

  it("prints nothing for a message with no time", () => {
    expect(messageTime(0, NOW)).toBe("");
    expect(dayLabel(new Date(NOW), new Date(NOW))).toBe("today");
  });
});

// ---- strings -----------------------------------------------------------------

describe("string helpers", () => {
  it("prints a prompt's opening line", () => {
    expect(firstLine("\n\nhttps://x.dev\n\ndo the thing")).toBe("https://x.dev");
    expect(firstLine("one line")).toBe("one line");
  });

  it("collapses home to ~ and names the folder", () => {
    expect(tildePath("/Users/me/x", "/Users/me")).toBe("~/x");
    expect(tildePath("/Users/me", "/Users/me/")).toBe("~");
    expect(tildePath("/opt/x", "/Users/me")).toBe("/opt/x");
    expect(tildePath("/opt/x", "")).toBe("/opt/x");
    expect(basename("/Users/me/x/")).toBe("x");
    expect(basename("/")).toBe("/");
  });
});
