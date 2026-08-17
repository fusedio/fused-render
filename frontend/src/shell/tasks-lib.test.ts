// The Tasks page's rules, without a DOM: the accordion's Show-more state, the
// per-message unread bookkeeping, the board's drag legality, filtering, and the
// one ordering promise the client makes (it keeps the server's).
import { describe, expect, it } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import type { Task, TaskMessage } from "@platform/lib/api";
import { BOARD_COLUMNS } from "./schedule-lib";
import type { BoardColumn } from "./schedule-lib";
import {
  ALL_MESSAGES,
  IMMINENT,
  JUST_NOW,
  EMPTY_FILTERS,
  LANE_SORTS,
  MESSAGE_ANCHOR_PARAM,
  PREVIEW_MESSAGES,
  UNREAD_LABEL,
  archiveIntent,
  basename,
  canCancel,
  canRunNow,
  cancelIntent,
  carryMarkToHeld,
  dayLabel,
  dropAction,
  dropLanes,
  filterTasks,
  firstLine,
  groupByColumn,
  heldMessages,
  isAllRead,
  isDraggable,
  isExpandable,
  isFailedTask,
  isPastDue,
  isUnread,
  laneTime,
  lastRunAt,
  markAllRead,
  markObservation,
  markRead,
  markReadIntent,
  messageEditEntry,
  messageHref,
  messageStamp,
  messageTime,
  messageTone,
  messageWhenTitle,
  nextRunAt,
  openMessageHref,
  openThreadIntent,
  projectOptions,
  relativeWhen,
  ranOffSchedule,
  readKey,
  settleMarkAllRead,
  resendTarget,
  runNowIntent,
  runNowTarget,
  soleMessage,
  spansProjects,
  sortLane,
  taskColumn,
  taskHref,
  taskRunIntent,
  taskUnread,
  taskUnreadLabel,
  taskWhen,
  threadView,
  tildePath,
  toggleExpanded,
  triageStatus,
  unmarkAllRead,
  unmarkRead,
  unreadMarker,
  upcomingEditEntry,
} from "./tasks-lib";

// 2026-08-16 is a Sunday; 2026-08-10 a Monday.
const NOW = Date.parse("2026-08-16T12:00:00");

function msg(over: Partial<TaskMessage> = {}): TaskMessage {
  return {
    message_id: "MSG-001",
    kind: "scheduled",
    body: "pull today's news",
    at: Math.floor(Date.parse("2026-08-16T09:00:00") / 1000),
    // Ran when it was due — the ordinary case, and the one that prints no note.
    ran_at: Math.floor(Date.parse("2026-08-16T09:00:00") / 1000),
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

/** What Show more fetches: the WHOLE thread, newest first, ids exactly as the
 * server formats them (`MSG-nnn`) so the listing's copy of a message and the
 * fetch's copy are the same message. */
function thread(n: number, over: Partial<TaskMessage> = {}): TaskMessage[] {
  const messages: TaskMessage[] = [];
  for (let i = n; i >= 1; i--) {
    messages.push(msg({ message_id: `MSG-${String(i).padStart(3, "0")}`, ...over }));
  }
  return messages;
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
    const view = threadView(t, thread(12));
    expect(view.messages.length).toBe(12);
    expect(new Set(view.messages.map((m) => m.message_id)).size).toBe(12);
    expect(view.more).toBe(false);
  });

  // The fetched thread is read ONCE and `more` is false afterwards, so nothing
  // ever refetches it — which used to freeze an expanded thread at the instant it
  // was fetched. The listing row is still polled, so it is the fresher of the two
  // about the three it carries, and anything it has that the fetch does not is a
  // message that arrived since.
  it("leads with a message that arrived after the fetch", () => {
    const fetched = thread(12);
    const t: Task = {
      ...task({ message_count: 13 }, 13),
      messages: [
        msg({ message_id: "MSG-013", unread: true }),
        msg({ message_id: "MSG-012" }),
        msg({ message_id: "MSG-011" }),
      ],
    };
    const view = threadView(t, fetched);
    expect(view.messages.map((m) => m.message_id).slice(0, 2))
      .toEqual(["MSG-013", "MSG-012"]);
    expect(view.messages.length).toBe(13);
    expect(new Set(view.messages.map((m) => m.message_id)).size).toBe(13);
  });

  it("takes the LISTING's copy of a message the fetch also holds", () => {
    // The fetch said unread; the poll since has seen it read. Two copies of one
    // message, and the newer answer is the one the row draws.
    const fetched = thread(12).map((m) =>
      m.message_id === "MSG-012" ? { ...m, unread: true } : m,
    );
    const t: Task = {
      ...task({}, 12),
      messages: [
        msg({ message_id: "MSG-012", unread: false }),
        msg({ message_id: "MSG-011" }),
        msg({ message_id: "MSG-010" }),
      ],
    };
    const view = threadView(t, fetched);
    expect(view.messages.length).toBe(12);
    expect(view.messages[0].message_id).toBe("MSG-012");
    expect(view.messages[0].unread).toBe(false);
  });

  it("holds nothing but the window before Show more has run", () => {
    const t = task({}, 12);
    expect(heldMessages(t).map((m) => m.message_id)).toEqual([
      "MSG-012", "MSG-011", "MSG-010",
    ]);
    expect(heldMessages(t, thread(12)).length).toBe(12);
  });

  it("trusts message_count, not the preview length, for 'is there more?'", () => {
    // A server that sent fewer than three but claims more still gets a button.
    expect(threadView({ ...task({}, 1), message_count: 4 }).more).toBe(true);
    // ...and one that claims fewer than it sent never goes negative.
    expect(threadView({ ...task({}, 3), message_count: 1 }).hidden).toBe(0);
  });
});

// ---- which rows are accordions at all ----------------------------------------
// "empty task (1 msg only) should not have dropdown" (Akshil, 2026-08-17). A
// thread of one drew a single message row under the task row whose title was the
// same words, so the chevron offered a press that revealed a restatement. Zero is
// the same case. The number asked has to be the SERVER's, or the answer is wrong
// on precisely the tasks with most to show.

describe("isExpandable", () => {
  it("offers no disclosure at zero or one message, and one from two up", () => {
    // Nothing to reveal: a pending task that has never run, and a thread whose one
    // message the row above is already showing.
    expect(isExpandable(task({}, 0))).toBe(false);
    expect(isExpandable(task({}, 1))).toBe(false);
    // Two is a thread.
    expect(isExpandable(task({}, 2))).toBe(true);
    expect(isExpandable(task({}, 3))).toBe(true);
    expect(isExpandable(task({}, 40))).toBe(true);
  });

  it("asks message_count, never the tail the client happens to hold", () => {
    // The listing sends at most PREVIEW_MESSAGES, and can send fewer or none at
    // all. Counting what we hold would call this forty-message task a leaf and
    // hide the chevron on the row with the most to open.
    expect(isExpandable({ ...task({}, 40), messages: [] })).toBe(true);
    expect(isExpandable({ ...task({}, 40), messages: [msg({})] })).toBe(true);
    // And the same number the Show more button is arithmetic over, so the chevron
    // and "Show N more" cannot disagree about the thread's length.
    const long = { ...task({}, 40), messages: [msg({})] };
    expect(threadView(long).more).toBe(true);
    // The converse: a tail longer than the count claims is still not a thread, so
    // the two never contradict each other in that direction either.
    const shrunk = { ...task({}, 3), message_count: 1 };
    expect(isExpandable(shrunk)).toBe(false);
    expect(threadView(shrunk).more).toBe(false);
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
    // A message the server already called read costs nothing when clicked — read
    // off a WINDOW (three of twelve), which is the arm this arithmetic serves.
    const window = { ...three(), unread: 12, message_count: 12 };
    const seen = {
      ...window,
      messages: window.messages.map((m) => ({ ...m, unread: false })),
    };
    expect(taskUnread(seen, markRead(read, t.key, "MSG-002"))).toBe(12);
  });

  // The marker LEADS the row now, and the word beside it is gone. Both halves
  // matter: the slot is asked about on every message (a blank one holds the
  // column open on read rows) and the word survives only as the dot's
  // accessible name.
  it("marks the row itself, and names it for a screen reader", () => {
    const t = three();
    expect(unreadMarker(t.key, t.messages[0], new Set())).toEqual({
      unread: true,
      label: UNREAD_LABEL,
    });
    expect(UNREAD_LABEL.toLowerCase()).toBe("unread");
  });

  it("returns a blank, unnamed marker for a read message", () => {
    const t = three();
    const read = markRead(new Set<string>(), t.key, "MSG-003");
    // Locally cleared...
    expect(unreadMarker(t.key, t.messages[0], read)).toEqual({ unread: false, label: "" });
    // ...and the server's own answer, once the poll catches up.
    const seen = msg({ message_id: "MSG-003", unread: false });
    expect(unreadMarker(t.key, seen, new Set())).toEqual({ unread: false, label: "" });
  });

  it("agrees with isUnread on every message, so the row cannot say two things", () => {
    const t = three();
    const read = markRead(new Set<string>(), t.key, "MSG-002");
    for (const m of t.messages) {
      expect(unreadMarker(t.key, m, read).unread).toBe(isUnread(t.key, m, read));
    }
  });

  it("does not go negative when the poll has already caught up", () => {
    // The window arm, where the count is the server's number less what we
    // discounted: it can be driven past zero and must clamp there.
    const t = { ...three(), unread: 0, message_count: 12 };
    expect(taskUnread(t, markRead(new Set<string>(), t.key, "MSG-003"))).toBe(0);
  });

  // Show more replaces the window with the whole thread, and the server's own
  // `unread` is deliberately approximate ("the Show-more endpoint is exact" —
  // _unread_count). So once we hold all of it, the count IS the dots: same
  // predicate, same list the rows are drawn from, and no way for the badge and
  // the rail to disagree.
  it("counts the thread itself once it holds every message of it", () => {
    const t = { ...three(), unread: 12, message_count: 3 };
    // Nothing marked: three flags, three dots, three on the badge — the server's
    // stale 12 does not get to outvote the thread in our hands.
    expect(taskUnread(t, new Set())).toBe(3);
    expect(taskUnread(t, markRead(new Set<string>(), t.key, "MSG-002"))).toBe(2);
  });

  it("keys reads per task — MSG-001 exists in every thread", () => {
    expect(readKey("a", "MSG-001")).not.toBe(readKey("b", "MSG-001"));
    const t = three();
    const other = markRead(new Set<string>(), "some-other-task", "MSG-003");
    expect(taskUnread(t, other)).toBe(3);
  });

  // The task row's own mark, which is now a DOT and nothing else (Akshil,
  // 2026-08-17: "only show a single dot like the notification that we show").
  // It printed a number for a day — `8`, `13`, `211` — and no reader ever spent
  // the digits: the only question a task's total answers is "is there anything
  // new in here?".
  it("says nothing at all when there is nothing unread", () => {
    // Null is what stops a dot being drawn: unlike the message row's old leading
    // slot there is no column here to hold open, so an empty mark would just be a
    // gap between the title and whatever follows it.
    expect(taskUnreadLabel(0)).toBe(null);
    expect(taskUnreadLabel(-1)).toBe(null);
  });

  it("keeps the real COUNT in the accessible name, uncapped, though nothing prints it", () => {
    // Dropping the number from the ink must not drop it from the name — that
    // would be losing the fact rather than not printing it. And there is no cap
    // any more: the old "99+" existed because three digits do not fit a 16px
    // chip, and a 7px dot has no such problem.
    expect(taskUnreadLabel(1)).toBe("1 unread message");
    expect(taskUnreadLabel(3)).toBe("3 unread messages");
    expect(taskUnreadLabel(211)).toBe("211 unread messages");
    expect(taskUnreadLabel(1234)).toBe("1234 unread messages");
  });

  it("names the task's mark the way it names a message's — for a reader", () => {
    // The message marker announces "Unread"; the task's announces how many are.
    // Both are names, neither is a bare glyph.
    expect(taskUnreadLabel(3)).toContain(UNREAD_LABEL.toLowerCase());
  });

  it("discounts against the LOADED thread once Show more has run", () => {
    const t = { ...three(), unread: 12, message_count: 12 };
    const full = [
      msg({ message_id: "MSG-012", unread: true }),
      msg({ message_id: "MSG-011", unread: true }),
    ];
    expect(taskUnread(t, markRead(new Set<string>(), t.key, "MSG-011"), full)).toBe(11);
  });

  // Clearing the WHOLE task, from the row's own button. The local half has to
  // cover messages this component has never held — the row lists three of 89 —
  // which is why it is one sentinel rather than an id per message.
  it("clears a whole task at once, including the messages it never held", () => {
    const t = { ...three(), unread: 89, message_count: 89 };
    const read = markAllRead(new Set<string>(), t);
    expect(isAllRead(read, t)).toBe(true);
    // Not 86: discounting only the loaded three would leave the row still
    // claiming most of a count the press just cleared.
    expect(taskUnread(t, read)).toBe(0);
    for (const m of t.messages) {
      expect(isUnread(t.key, m, read)).toBe(false);
      expect(unreadMarker(t.key, m, read).unread).toBe(false);
    }
  });

  it("clears only the task it was asked about", () => {
    const t = three();
    const elsewhere = markAllRead(new Set<string>(), { ...three(), key: "other" });
    expect(taskUnread(t, elsewhere)).toBe(3);
    expect(isUnread(t.key, t.messages[0], elsewhere)).toBe(true);
  });

  it("keeps the whole-task mark and a per-message one apart", () => {
    // The sentinel occupies the message-id slot, so it must be a shape no thread
    // can produce — and marking one message must never read as marking all.
    expect(ALL_MESSAGES).not.toMatch(/^MSG-/);
    const t = three();
    expect(isAllRead(markRead(new Set<string>(), t.key, "MSG-003"), t)).toBe(false);
    expect(taskUnread(t, markRead(new Set<string>(), t.key, "MSG-003"))).toBe(2);
  });
});

// ---- the whole-task mark, and what retires it ---------------------------------
// The optimism used to be a lasting `*` sentinel that isUnread and taskUnread
// read as absolute, with nothing that ever removed it: a refused write left the
// row looking read with its own Mark read button gone, a server still reporting
// unread was ignored, and a message arriving afterwards was invisible until the
// List remounted. It is an override of a KNOWN-STALE VALUE, so it is stamped
// with that value and it can be taken back.

describe("the whole-task mark", () => {
  const long = (over: Partial<Task> = {}): Task => ({
    ...task({ unread: 89, message_count: 89 }, 3),
    messages: [
      msg({ message_id: "MSG-089", unread: true }),
      msg({ message_id: "MSG-088", unread: true }),
      msg({ message_id: "MSG-087", unread: true }),
    ],
    ...over,
  });

  it("clears instantly — the whole point, and the 20s poll is what it hides", () => {
    const t = long();
    const read = markAllRead(new Set<string>(), t);
    // The count goes, including the 86 outside the window...
    expect(taskUnread(t, read)).toBe(0);
    // ...the dots of the three it holds go...
    for (const m of t.messages) expect(unreadMarker(t.key, m, read).unread).toBe(false);
    // ...and the button removes itself on its own press.
    expect(markReadIntent(t, read)).toBe(null);
  });

  it("puts the dots AND the button back when the write is refused", () => {
    const t = long();
    const marked = markAllRead(new Set<string>(), t);
    // What the component does in its catch: roll back what the press wrote.
    const back = unmarkAllRead(marked, t.key, t.messages);
    expect(taskUnread(t, back)).toBe(89);
    for (const m of t.messages) expect(unreadMarker(t.key, m, back).unread).toBe(true);
    // Without this the row had no retry at all: the count was 0, so the button
    // that would have tried again was not drawn.
    expect(markReadIntent(t, back)!.unread).toBe(89);
  });

  it("rolls back the sentinel whatever observation it was stamped with", () => {
    // A poll landed while the request was in flight, so the mark is already
    // inert — but inert is not gone, and the same numbers could come round again.
    const t = long();
    const marked = markAllRead(new Set<string>(), t);
    const back = unmarkAllRead(marked, t.key, t.messages);
    expect([...back].some((k) => k.includes(ALL_MESSAGES))).toBe(false);
  });

  it("lets a poll that still reports unread win", () => {
    const t = long();
    const read = markAllRead(new Set<string>(), t);
    // The next poll is a FRESH read of the server: it marked the three we held
    // and something else is unread. Nothing about the earlier press may hide it.
    const polled: Task = {
      ...long(),
      unread: 4,
      messages: [
        msg({ message_id: "MSG-093", unread: true }),
        msg({ message_id: "MSG-089", unread: false }),
        msg({ message_id: "MSG-088", unread: false }),
      ],
    };
    expect(isAllRead(read, polled)).toBe(false);
    expect(taskUnread(polled, read)).toBe(4);
    expect(markReadIntent(polled, read)).not.toBe(null);
  });

  it("shows a message that arrives AFTER the mark, without a remount", () => {
    const t = long();
    const read = markAllRead(new Set<string>(), t);
    const arrived = msg({ message_id: "MSG-090", unread: true });
    // The id is one the press never wrote, so the dot draws on its own — this is
    // what the wildcard could not do, because it could not name a message.
    expect(isUnread(t.key, arrived, read)).toBe(true);
    expect(unreadMarker(t.key, arrived, read).unread).toBe(true);
    // And the row it lands on counts it: the observation the mark was stamped
    // with is not the one the server is quoting any more.
    const polled: Task = { ...long(), unread: 1, messages: [
      arrived,
      msg({ message_id: "MSG-089", unread: false }),
      msg({ message_id: "MSG-088", unread: false }),
    ] };
    expect(taskUnread(polled, read)).toBe(1);
  });

  it("keeps the mark while the server is still quoting the value it overrode", () => {
    // The poll that predates the write says exactly what the press corrected, and
    // that one is the whole reason the local set exists.
    const t = long();
    const read = markAllRead(new Set<string>(), t);
    expect(taskUnread(long(), read)).toBe(0);
    // A different count is a different read, even when it is HIGHER.
    expect(taskUnread({ ...long(), unread: 90 }, read)).toBeGreaterThan(0);
  });

  it("stamps the count AND the ids, so a swap does not read as no change", () => {
    const t = long();
    const one = markObservation(t);
    // Same count, different set: one was read, one arrived.
    const swapped: Task = { ...long(), messages: [
      msg({ message_id: "MSG-090", unread: true }),
      msg({ message_id: "MSG-089", unread: true }),
      msg({ message_id: "MSG-088", unread: true }),
    ] };
    expect(markObservation(swapped)).not.toBe(one);
    expect(isAllRead(markAllRead(new Set<string>(), t), swapped)).toBe(false);
  });

  it("reads the server's own answer to the mark, instead of dropping it", () => {
    const t = long();
    const marked = markAllRead(new Set<string>(), t);
    // 0 left: the optimism was right, and nothing moves.
    expect(settleMarkAllRead(marked, t.key, t.messages, { unread: 0 })).toBe(marked);
    expect(taskUnread(t, settleMarkAllRead(marked, t.key, t.messages, { unread: 0 })))
      .toBe(0);
    // Something arrived while the request was in flight. The server says the row
    // is not clear, so the row says so too — over-reporting for one poll can only
    // show news that exists; hiding it cannot.
    const settled = settleMarkAllRead(marked, t.key, t.messages, { unread: 2 });
    expect(taskUnread(t, settled)).toBe(89);
    expect(markReadIntent(t, settled)).not.toBe(null);
  });

  it("leaves the per-message mark alone — it was already sound", () => {
    // Concrete id, not a wildcard: it cannot hide a message it has never named,
    // and it retires itself once the server agrees. What it lacked was the way
    // back, which is unmarkRead.
    const t = long();
    const read = markRead(new Set<string>(), t.key, "MSG-089");
    expect(isUnread(t.key, t.messages[0], read)).toBe(false);
    expect(isUnread(t.key, msg({ message_id: "MSG-090", unread: true }), read)).toBe(true);
    // The server has caught up: the local entry stops discounting anything
    // rather than double-counting the message it was about.
    const caught: Task = { ...long(), unread: 88, messages: [
      msg({ message_id: "MSG-089", unread: false }),
      msg({ message_id: "MSG-088", unread: true }),
      msg({ message_id: "MSG-087", unread: true }),
    ] };
    expect(taskUnread(caught, read)).toBe(88);
    // And the refused write gives the dot back.
    const back = unmarkRead(read, t.key, "MSG-089");
    expect(isUnread(t.key, t.messages[0], back)).toBe(true);
    expect(taskUnread(t, back)).toBe(89);
  });

  it("never mutates the set it was handed", () => {
    const t = long();
    const before = new Set<string>();
    const marked = markAllRead(before, t);
    expect(before.size).toBe(0);
    unmarkAllRead(marked, t.key, t.messages);
    expect(marked.size).toBeGreaterThan(0);
    expect(isAllRead(marked, t)).toBe(true);
  });
});

// ---- the mark, and the thread Show more fetched ---------------------------------
// The bug the fix above left behind. Its two halves were each right: isUnread
// stopped consulting the sentinel (a wildcard cannot name a message, which is why
// an arrival was invisible), and the OBSERVATION is stamped off the listing row
// only (stamping the fetched thread would retire a mark that is still true). What
// was missed is that the sentinel and the concrete ids answer different questions:
// the observation is what the SERVER LAST SAID, the ids are what is ON SCREEN. So
// pressing Show more and then Mark read zeroed the count through the sentinel and
// left 86 lit dots that no key could ever take back — and `more` is false by then,
// so nothing refetched them.

describe("the whole-task mark over a fetched thread", () => {
  /** The listing row for a long thread: three of 89, all unread. */
  const listing = (over: Partial<Task> = {}): Task => ({
    ...task({ unread: 89, message_count: 89 }, 3),
    messages: [
      msg({ message_id: "MSG-089", unread: true }),
      msg({ message_id: "MSG-088", unread: true }),
      msg({ message_id: "MSG-087", unread: true }),
    ],
    ...over,
  });
  /** What Show more brings back before the mark: all 89, all unread. */
  const fetched = () => thread(89, { unread: true });

  it("Show more then Mark read leaves NO dot in the thread, and a zero count", () => {
    const t = listing();
    const held = heldMessages(t, fetched());
    expect(held.length).toBe(89);
    const read = markAllRead(new Set<string>(), t, held);
    expect(taskUnread(t, read, held)).toBe(0);
    for (const m of held) expect(unreadMarker(t.key, m, read).unread).toBe(false);
    expect(markReadIntent(t, read, held)).toBe(null);
    // And the witness for why `held` is passed at all: ids off the listing window
    // clear three of 89 and the sentinel zeroes the count over the other 86.
    const narrow = markAllRead(new Set<string>(), t);
    expect(held.filter((m) => isUnread(t.key, m, narrow)).length).toBe(86);
    expect(taskUnread(t, narrow, held)).toBe(0);
  });

  it("Mark read then Show more does not resurrect the dots the mark covered", () => {
    // The reverse order, and the same lie from the other side: the fetch is a READ
    // of the value the press overrode — the server had not applied the write when
    // it composed this thread — so 86 messages arrive flagged unread.
    const t = listing();
    const marked = markAllRead(new Set<string>(), t);
    const thread89 = fetched();
    const carried = carryMarkToHeld(marked, t, thread89);
    const held = heldMessages(t, thread89);
    for (const m of held) expect(unreadMarker(t.key, m, carried).unread).toBe(false);
    expect(taskUnread(t, carried, held)).toBe(0);
    // Without it, the 86 light up under a row that says 0 — a dot back on a message
    // the reader marked read a second ago.
    expect(held.filter((m) => isUnread(t.key, m, marked)).length).toBe(86);
  });

  it("adopts through the SENTINEL's observation, and widens nothing", () => {
    // The gate is "is the server still quoting the value the press overrode?",
    // which is exactly the question. The sentinel it re-stamps is the one that
    // just answered it, so the observation is never widened to the fetched thread.
    const t = listing();
    const marked = markAllRead(new Set<string>(), t);
    const carried = carryMarkToHeld(marked, t, fetched());
    expect(isAllRead(carried, t)).toBe(true);
    expect([...carried].filter((k) => k.includes(ALL_MESSAGES)).length).toBe(1);
    expect(markObservation(t)).toBe("89\u0000MSG-087,MSG-088,MSG-089");
  });

  it("adopts NOTHING once the mark has expired, so news stays news", () => {
    // A poll disagreed (or the answer rolled the mark back): there is no mark to
    // carry, and a fetch is just a fetch. Same set back, untouched.
    const t = listing();
    const marked = markAllRead(new Set<string>(), t);
    const polled = listing({ unread: 4, messages: [
      msg({ message_id: "MSG-093", unread: true }),
      msg({ message_id: "MSG-089", unread: false }),
      msg({ message_id: "MSG-088", unread: false }),
    ] });
    expect(carryMarkToHeld(marked, polled, fetched())).toBe(marked);
    const rolled = unmarkAllRead(marked, t.key, t.messages);
    expect(carryMarkToHeld(rolled, t, fetched())).toBe(rolled);
  });

  it("still dots AND counts a message that arrives after the mark", () => {
    // The defect the sentinel was rebuilt to stop having, asked of an EXPANDED
    // thread: the fetch is never refetched, so the arrival can only come through
    // the listing row — heldMessages leads with it, and the count is the dots.
    const t = listing();
    const thread89 = fetched();
    const read = markAllRead(new Set<string>(), t, heldMessages(t, thread89));
    const arrived = msg({ message_id: "MSG-090", unread: true });
    const polled = listing({ unread: 1, message_count: 90, messages: [
      arrived,
      msg({ message_id: "MSG-089", unread: false }),
      msg({ message_id: "MSG-088", unread: false }),
    ] });
    const held = heldMessages(polled, thread89);
    expect(held[0].message_id).toBe("MSG-090");
    expect(unreadMarker(polled.key, held[0], read).unread).toBe(true);
    expect(taskUnread(polled, read, held)).toBe(1);
    expect(markReadIntent(polled, read, held)!.unread).toBe(1);
  });

  it("gives the WHOLE fetched thread back when the write is refused", () => {
    const t = listing();
    const held = heldMessages(t, fetched());
    const read = markAllRead(new Set<string>(), t, held);
    const back = unmarkAllRead(read, t.key, held);
    expect(taskUnread(t, back, held)).toBe(89);
    for (const m of held) expect(unreadMarker(t.key, m, back).unread).toBe(true);
    expect(markReadIntent(t, back, held)!.unread).toBe(89);
    // Same for the server's own answer: something arrived while it was marking, so
    // the row goes back to reporting the thread rather than swallowing it.
    expect(taskUnread(t, settleMarkAllRead(read, t.key, held, { unread: 2 }), held))
      .toBe(89);
  });

  it("rolls back what a Show more adopted WHILE the write was in flight", () => {
    // The press wrote ids for the window it held; the fetch that landed a moment
    // later carried the same mark onto the other 86. Both are the press's, so a
    // rollback has to be handed both — which is why the caller passes what it
    // wrote AND what the thread holds now.
    const t = listing();
    const wrote = heldMessages(t);
    let read = markAllRead(new Set<string>(), t, wrote);
    read = carryMarkToHeld(read, t, fetched());
    const now = heldMessages(t, fetched());
    const back = unmarkAllRead(read, t.key, [...wrote, ...now]);
    for (const m of now) expect(unreadMarker(t.key, m, back).unread).toBe(true);
    expect(taskUnread(t, back, now)).toBe(89);
    // The narrow rollback is the same half-restored row wearing the other hat: 86
    // messages stay silently read, with nothing left that could relight them.
    const narrow = unmarkAllRead(read, t.key, wrote);
    expect(now.filter((m) => !isUnread(t.key, m, narrow)).length).toBe(86);
  });

  it("never mutates the set it was handed", () => {
    const t = listing();
    const marked = markAllRead(new Set<string>(), t);
    const size = marked.size;
    carryMarkToHeld(marked, t, fetched());
    expect(marked.size).toBe(size);
  });
});

// ---- clearing a task without opening it ----------------------------------------
// Read state is per message and clearing it was per message too, so "I have seen
// all of this" cost one click per row (Akshil, 2026-08-17: "so you don't have to
// open everything individually").

describe("markReadIntent", () => {
  // Flags consistent with the count, because a row that holds its whole thread
  // is counted off the flags themselves (taskUnread): a fixture saying "1 unread"
  // over three lit messages is a server contradicting itself.
  const withUnread = (n: number): Task => ({
    ...task({ unread: n, message_count: Math.max(n, 3) }, 3),
    messages: [
      msg({ message_id: "MSG-003", unread: n >= 1 }),
      msg({ message_id: "MSG-002", unread: n >= 2 }),
      msg({ message_id: "MSG-001", unread: n >= 3 }),
    ],
  });

  it("is offered only on a task that actually has unread", () => {
    // No unread, no button: unlike Archive, this one's press would do nothing,
    // and a glyph on every row is what makes the rows that matter hard to find.
    expect(markReadIntent(task({ unread: 0 }), new Set())).toBe(null);
    expect(markReadIntent(withUnread(3), new Set())).not.toBe(null);
  });

  it("says how much it clears, because the row only ever lists three", () => {
    const many = markReadIntent(withUnread(89), new Set())!;
    expect(many.label).toBe("Mark read");
    expect(many.unread).toBe(89);
    expect(many.title).toContain("89");
    // One reads as one rather than as "all 1".
    expect(markReadIntent(withUnread(1), new Set())!.title)
      .toContain("1 unread message");
  });

  it("leaves on its own press rather than on the next poll", () => {
    // It asks the count the row is DRAWING (taskUnread), so the local mark the
    // click writes is enough to take the button away.
    const t = withUnread(3);
    expect(markReadIntent(t, markAllRead(new Set<string>(), t))).toBe(null);
    // ...and clicking through every message it holds does the same.
    let read = new Set<string>();
    for (const m of t.messages) read = markRead(read, t.key, m.message_id);
    expect(markReadIntent(t, read)).toBe(null);
  });

  it("counts against the loaded thread once Show more has run", () => {
    const t = { ...withUnread(12), message_count: 12 };
    const full = [
      msg({ message_id: "MSG-012", unread: true }),
      msg({ message_id: "MSG-011", unread: true }),
    ];
    expect(markReadIntent(t, markRead(new Set<string>(), t.key, "MSG-011"), full)!.unread)
      .toBe(11);
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

  it("speaks the board's five words, not a list of its own", () => {
    // The fifth arrived a round after the first four; a hardcoded list here is
    // how a real lane silently empties itself into Done.
    for (const col of BOARD_COLUMNS) {
      expect(taskColumn({ ...task(), status: col.key as Task["status"] })).toBe(col.key);
    }
    expect(BOARD_COLUMNS.map((c) => c.key)).toEqual([
      "upcoming", "in_progress", "done", "failed", "archived",
    ]);
  });
});

// ---- drag --------------------------------------------------------------------

/** An upcoming task holding `pending` messages at the given epoch-second times,
 * newest first, as the server sends them. */
function upcoming(dues: number[], over: Partial<Task> = {}): Task {
  const messages = [...dues]
    .sort((a, b) => b - a)
    .map((at, i) =>
      msg({
        message_id: `MSG-${String(dues.length - i).padStart(3, "0")}`,
        state: "pending",
        entry_id: `e${dues.length - i}`,
        at,
        ran_at: 0, // it has not run: that is what pending means
      }),
    );
  return task({ status: "upcoming", message_count: messages.length, messages, ...over });
}

const T9 = Math.floor(Date.parse("2026-08-17T09:00:00") / 1000);
const T18 = Math.floor(Date.parse("2026-08-17T18:00:00") / 1000);

/** The fifth status. Cast in ONE place because `Task.status` in api.ts is still
 * the four-word union this round started with — the server writes the word and
 * the board already draws its lane. The cast can go the moment that union
 * gains it; nothing else in this file has to change when it does. */
const FAILED = "failed" as Task["status"];

describe("dropLanes", () => {
  it("a task with no session has nothing to triage, so it cannot lift", () => {
    // ...and nothing pending either, so there is nothing to run early.
    const pending = task({ key: "pending:e1", session_id: "", status: "upcoming" });
    expect(dropLanes(pending)).toEqual([]);
    expect(isDraggable(pending)).toBe(false);
  });

  it("offers the other two triage lanes, never the one it is already in", () => {
    expect(dropLanes(task({ status: "done" }))).toEqual(["in_progress", "archived"]);
    expect(dropLanes(task({ status: "in_progress" }))).toEqual(["done", "archived"]);
    expect(isDraggable(task({ status: "done" }))).toBe(true);
  });

  it("refuses to send a lane setSessionTriage does not accept", () => {
    expect(triageStatus("upcoming")).toBe(null);
    expect(triageStatus("done")).toBe("done");
  });

  // Upcoming → In Progress is a RUN, not a filing (Akshil, 2026-08-16). Its
  // precondition is therefore a message to send, not a session to file under.
  it("lets a never-run scheduled task into In Progress: run needs no session", () => {
    const t = upcoming([T9], { key: "pending:e1", session_id: "" });
    expect(dropLanes(t)).toEqual(["in_progress"]);
    expect(isDraggable(t)).toBe(true);
  });

  it("refuses In Progress for an upcoming task with nothing pending", () => {
    // A pure-chat task has no scheduled message anywhere in it — there is
    // nothing to fire — so the drop is illegal BEFORE the card lands rather
    // than a call that fails after it.
    const chat = task({ status: "upcoming" }); // factory messages are `sent`
    expect(dropLanes(chat)).toEqual(["done", "archived"]);
    // Same for one whose only scheduled message was already cancelled.
    const dead = task({
      status: "upcoming",
      messages: [msg({ state: "cancelled" })],
    });
    expect(dropLanes(dead).includes("in_progress")).toBe(false);
  });

  // Failed is the fifth lane and it is asymmetric: nothing goes in, and out of
  // it there are exactly two moves.
  it("never offers Failed as a destination, from any lane", () => {
    for (const status of ["upcoming", "in_progress", "done", "archived"] as const) {
      expect(dropLanes(task({ status })).includes("failed")).toBe(false);
    }
    expect(dropLanes(upcoming([T9])).includes("failed")).toBe(false);
  });

  it("lets a failed task out to In Progress (re-run) and Archive, never Done", () => {
    // A run that broke did not finish, so filing it as Done would put back the
    // exact lie the lane exists to remove.
    const retryable = upcoming([T9], { status: FAILED });
    expect(dropLanes(retryable)).toEqual(["in_progress", "archived"]);
    // With nothing pending there is nothing to re-run — but it can still be
    // filed away.
    const spent = task({ status: FAILED });
    expect(dropLanes(spent)).toEqual(["archived"]);
    expect(isDraggable(spent)).toBe(true);
  });

  it("still refuses In Progress on a pending message with no entry id", () => {
    const t = upcoming([T9]);
    const orphan = { ...t, messages: [{ ...t.messages[0], entry_id: "" }] };
    expect(dropLanes(orphan).includes("in_progress")).toBe(false);
  });
});

describe("runNowTarget", () => {
  it("picks the EARLIEST due of several pending messages", () => {
    const t = upcoming([T18, T9]);
    expect(runNowTarget(t)?.at).toBe(T9);
    expect(canRunNow(t)).toBe(true);
  });

  it("ignores messages that are not pending", () => {
    const t = task({
      status: "upcoming",
      messages: [
        msg({ message_id: "MSG-003", state: "pending", entry_id: "e3", at: T18 }),
        // Earlier, but already gone out: not a candidate.
        msg({ message_id: "MSG-002", state: "sent", entry_id: "e2", at: T9 }),
      ],
    });
    expect(runNowTarget(t)?.messageId).toBe("MSG-003");
  });

  it("takes the older message when two are due at the same second", () => {
    const t = upcoming([T9, T9]);
    expect(runNowTarget(t)?.messageId).toBe("MSG-001");
  });

  it("is null for a task with nothing pending", () => {
    expect(runNowTarget(task())).toBe(null);
    expect(canRunNow(task())).toBe(false);
  });

  it("fires the run the ROW NAMES when the window does not hold it", () => {
    // The half that used to be missing. The overdue pending is outside the three
    // newest by `at`, so the only pending message the window shows is next
    // month's occurrence — and the lane now orders this card by the overdue one
    // (nextRunAt reads `next_run`), so the button has to send THAT entry or the
    // order is a lie.
    const t = task({
      status: "upcoming",
      message_count: 40,
      next_run: SEC("2026-08-14T09:00:00"),
      next_run_entry: "e-overdue",
      messages: [
        due("2026-10-01T09:00:00", { message_id: "MSG-040", entry_id: "e-oct" }),
        ran("2026-08-15T09:00:00", "2026-08-15T09:00:00", { message_id: "MSG-039" }),
        ran("2026-08-14T09:00:00", "2026-08-14T09:00:00", { message_id: "MSG-038" }),
      ],
    });
    expect(runNowTarget(t)).toEqual({
      entryId: "e-overdue",
      // No id: MSG-n is a position in the whole thread and the row never parsed
      // this message. Nothing in the run path needs one.
      messageId: "",
      at: SEC("2026-08-14T09:00:00"),
    });
    // The sort and the button, on the same instant. That IS the fix.
    expect(runNowTarget(t)!.at).toBe(nextRunAt(t)!);
    expect(runNowIntent(t)!.entryId).toBe("e-overdue");
    expect(dropAction(t, "in_progress")).toEqual({
      kind: "run",
      entryId: "e-overdue",
      messageId: "",
    });
  });

  it("keeps the HELD message when the row names a run it also holds", () => {
    // Ordinary case: the named next run is in the window, so it is fired as the
    // message it is and keeps its printed id.
    const t = upcoming([T18, T9], { next_run: T9, next_run_entry: "e1" });
    expect(runNowTarget(t)).toEqual({ entryId: "e1", messageId: "MSG-001", at: T9 });
  });

  it("ignores a named run the row cannot fire, and one that is not there", () => {
    // Half a fact is not a fact: a time with no entry is a run the button cannot
    // send, so it names nothing and the window answers. Same for `next_run: 0`,
    // which is how the server says "nothing pending".
    const t = upcoming([T18, T9], { next_run: SEC("2026-08-14T09:00:00") });
    expect(runNowTarget(t)?.entryId).toBe("e1");
    expect(runNowTarget(upcoming([T9], { next_run: 0, next_run_entry: "" }))?.entryId)
      .toBe("e1");
    // And a named run on a task with nothing pending in the window at all is
    // still runnable: that is the whole point of the field.
    const outside = task({
      status: "upcoming",
      next_run: T9,
      next_run_entry: "e-hidden",
      messages: [ran("2026-08-15T09:00:00")],
    });
    expect(canRunNow(outside)).toBe(true);
    expect(runNowTarget(outside)?.entryId).toBe("e-hidden");
  });
});

describe("dropAction", () => {
  it("reads Upcoming → In Progress as a run of the earliest pending message", () => {
    const t = upcoming([T18, T9]);
    expect(dropAction(t, "in_progress")).toEqual({
      kind: "run",
      entryId: "e1",
      messageId: "MSG-001",
    });
  });

  it("reads every other legal drop as a triage write", () => {
    expect(dropAction(upcoming([T9]), "archived")).toEqual({
      kind: "triage",
      status: "archived",
    });
    expect(dropAction(task({ status: "done" }), "in_progress")).toEqual({
      kind: "triage",
      status: "in_progress",
    });
  });

  it("reads Failed → In Progress as a re-run, not a triage write", () => {
    expect(dropAction(upcoming([T9], { status: FAILED }), "in_progress")).toEqual({
      kind: "run",
      entryId: "e1",
      messageId: "MSG-001",
    });
    // ...and Failed → Archive as an ordinary filing.
    expect(dropAction(upcoming([T9], { status: FAILED }), "archived")).toEqual({
      kind: "triage",
      status: "archived",
    });
  });

  it("is null for anything dropLanes would not have allowed", () => {
    // The lane it is already in, a lane triage cannot express, and the run lane
    // on a task with nothing to run.
    expect(dropAction(task({ status: "done" }), "done")).toBe(null);
    expect(dropAction(task({ status: "done" }), "upcoming")).toBe(null);
    expect(dropAction(task({ status: "upcoming" }), "in_progress")).toBe(null);
    expect(dropAction(task({ status: "done" }), "failed")).toBe(null);
    expect(dropAction(upcoming([T9], { status: FAILED }), "done")).toBe(null);
    // A never-run task may run, but may not be filed.
    const fresh = upcoming([T9], { key: "pending:e1", session_id: "" });
    expect(dropAction(fresh, "archived")).toBe(null);
  });
});

// ---- Run now / Re-run ---------------------------------------------------------
// The drag from Upcoming into In Progress, reachable without a drag (Akshil,
// 2026-08-17). One call, one target rule, two words.

describe("runNowIntent", () => {
  it("offers nothing on a task with no runnable message", () => {
    // Nothing pending: the factory's messages have already been sent.
    expect(runNowIntent(task({ status: "upcoming" }))).toBe(null);
    expect(runNowIntent(task({ status: "done" }))).toBe(null);
    // Pending, but with no entry to claim — the call has nothing to send.
    const t = upcoming([T9]);
    expect(runNowIntent({ ...t, messages: [{ ...t.messages[0], entry_id: "" }] })).toBe(null);
    // And it agrees with the predicate the drag path uses.
    expect(canRunNow(task({ status: "upcoming" }))).toBe(false);
  });

  it("says Run now on an upcoming task", () => {
    const intent = runNowIntent(upcoming([T9]))!;
    expect(intent.label).toBe("Run now");
    expect(intent.rerun).toBe(false);
    // The tooltip says the half a person would otherwise fear: the schedule is
    // not being rewritten to this minute.
    expect(intent.title).toContain("stays put");
  });

  it("says Re-run on a failed one — same call, different word", () => {
    const failed = runNowIntent(upcoming([T9], { status: FAILED }))!;
    expect(failed.label).toBe("Re-run");
    expect(failed.rerun).toBe(true);
    // ...and the same on a task that is filed in Done but wearing the red ring:
    // the row says "Failed" in both cases, so the button uses the same verb.
    const flagged = runNowIntent(upcoming([T9], { status: "done", failed: true }))!;
    expect(flagged.label).toBe("Re-run");
    expect(isFailedTask(upcoming([T9], { status: FAILED }))).toBe(true);
    expect(isFailedTask(upcoming([T9]))).toBe(false);
  });

  it("the label is the ONLY thing that differs by status", () => {
    const early = runNowIntent(upcoming([T18, T9]))!;
    const again = runNowIntent(upcoming([T18, T9], { status: FAILED }))!;
    expect(early.entryId).toBe(again.entryId);
    expect(early.messageId).toBe(again.messageId);
    expect(early.label).not.toBe(again.label);
  });

  it("fires the same message the DRAG would have fired", () => {
    // The whole point of reusing runNowTarget: a button that picked a second
    // way would send a different message than the drop on the same card.
    for (const t of [
      upcoming([T18, T9]),
      upcoming([T9, T9]),
      upcoming([T9], { key: "pending:e1", session_id: "" }),
      upcoming([T18, T9], { status: FAILED }),
    ]) {
      const intent = runNowIntent(t)!;
      expect(intent.messageId).toBe(runNowTarget(t)!.messageId);
      expect(dropAction(t, "in_progress")).toEqual({
        kind: "run",
        entryId: intent.entryId,
        messageId: intent.messageId,
      });
    }
  });

  it("has nothing to fire on a failed task whose run is already spent", () => {
    // The common failure: the run went out and broke, so there is no PENDING
    // message left to claim and run-now has nothing to fire. This function
    // still says so — it answers only the run-now question, which is what lets
    // the drag keep asking it. Offering the button anyway is taskRunIntent's
    // job, and it does it with the OTHER call (resend).
    const spent = task({ status: FAILED });
    expect(canRunNow(spent)).toBe(false);
    expect(runNowIntent(spent)).toBe(null);
    // A repeat, though, has its next occurrence pending and IS re-runnable.
    expect(runNowIntent(upcoming([T9], { status: FAILED }))).not.toBe(null);
  });
});

// ---- which call the one button makes ------------------------------------------
// Re-run has to work in the case it was asked for: a task that ran and broke,
// which by then has no pending message left to claim. Two server verbs, one
// button, and the choice between them is a pure function so it cannot drift
// from what the drag does.

describe("taskRunIntent", () => {
  /** A failed task whose only message already ran and broke — the shape that
   * had no button at all before resend existed. */
  const broke = (over: Partial<Task> = {}) =>
    task({
      status: FAILED,
      failed: true,
      messages: [msg({ state: "error", entry_id: "e1" })],
      ...over,
    });

  it("routes a failed task with a pending message to run-now", () => {
    // Pending beats spent: the user already asked for that one and has not had
    // it, so bringing it forward is the smaller and truer action.
    const t = upcoming([T9], { status: FAILED });
    const intent = taskRunIntent(t)!;
    expect(intent.kind).toBe("run-now");
    expect(intent.label).toBe("Re-run");
    // ...and it is the very message the drag would have fired.
    expect(intent.entryId).toBe(runNowIntent(t)!.entryId);
    expect(dropAction(t, "in_progress")).toEqual({
      kind: "run",
      entryId: intent.entryId,
      messageId: intent.messageId,
    });
  });

  it("routes a failed task with nothing pending to resend", () => {
    const intent = taskRunIntent(broke())!;
    expect(intent.kind).toBe("resend");
    expect(intent.label).toBe("Re-run");
    expect(intent.rerun).toBe(true);
    // The entry it names is the one that ALREADY RAN. The server copies it and
    // leaves it alone; nothing here rewrites the run that broke.
    expect(intent.entryId).toBe("e1");
    expect(intent.messageId).toBe("MSG-001");
    expect(intent.title).toContain("same thread");
  });

  it("offers neither on a task that is not failed", () => {
    // Done, with its run finished. Re-asking for work that succeeded is a chat
    // message, not a button — and run-now has nothing pending to claim.
    expect(taskRunIntent(task({ status: "done" }))).toBe(null);
    expect(taskRunIntent(task({ status: "in_progress" }))).toBe(null);
    expect(taskRunIntent(task({ status: "archived" }))).toBe(null);
  });

  it("still says Run now, and nothing about re-sending, on an upcoming task", () => {
    const intent = taskRunIntent(upcoming([T18, T9]))!;
    expect(intent.kind).toBe("run-now");
    expect(intent.label).toBe("Run now");
    expect(intent.rerun).toBe(false);
  });

  it("re-sends the NEWEST run that ended, and never one that did not", () => {
    // Newest first, as the server sends them: asking again means asking for the
    // last thing that was asked for.
    const t = broke({
      messages: [
        msg({ message_id: "MSG-003", state: "error", entry_id: "e3" }),
        msg({ message_id: "MSG-002", state: "sent", entry_id: "e2" }),
        msg({ message_id: "MSG-001", state: "sent", entry_id: "e1" }),
      ],
    });
    expect(resendTarget(t)!.entry_id).toBe("e3");
    expect(taskRunIntent(t)!.entryId).toBe("e3");

    // A message that never went has nothing to send again — the same rule the
    // server enforces, so the button is not drawn onto a call that would 409.
    for (const state of ["cancelled", "missed", "skipped"] as const) {
      expect(
        resendTarget(broke({ messages: [msg({ state, entry_id: "e1" })] })),
      ).toBe(null);
    }
    // A chat message carries no schedule entry: it was delivered when it was
    // typed and there is nothing stored to copy.
    expect(
      taskRunIntent(broke({ messages: [msg({ kind: "chat", entry_id: "" })] })),
    ).toBe(null);
  });

  it("leaves the DRAG on run-now only", () => {
    // A drop on a lane says where the card belongs; it must not quietly create
    // work that was never scheduled. So the failed task with nothing pending —
    // the one case resend exists for — cannot be dragged into In Progress at
    // all, exactly as before.
    const spent = broke();
    expect(dropLanes(spent)).toEqual(["archived"]);
    expect(dropAction(spent, "in_progress")).toBe(null);
    // And every legal drop is still a run or a triage write, never a resend.
    for (const lane of ["in_progress", "done", "archived"] as const) {
      const action = dropAction(upcoming([T9], { status: FAILED }), lane);
      if (action) expect(["run", "triage"]).toContain(action.kind);
    }
  });
});

// ---- filing it away ----------------------------------------------------------
// "Can a task be deleted?" is answered "no — it is archived, the transcript is
// kept" (D306), and that answer is only true while archiving is reachable. It
// used to be one gesture on one view, onto a lane that starts COLLAPSED.

describe("archiveIntent", () => {
  it("offers Archive on a task that has run", () => {
    const a = archiveIntent(task({ status: "done" }))!;
    expect(a.label).toBe("Archive");
    expect(a.status).toBe("archived");
    expect(a.lane).toBe("archived");
    expect(a.restore).toBe(false);
    // The half a person reaching for Delete is actually asking about.
    expect(a.title).toContain("kept");
  });

  it("offers the way BACK on a task already archived", () => {
    // Archiving with no way out is a trap, and the Board's drag can already
    // pull a card out of Archive — so the List must not be a one-way door.
    const a = archiveIntent(task({ status: "archived" }))!;
    expect(a.label).toBe("Unarchive");
    expect(a.status).toBe("in_progress");
    expect(a.lane).toBe("in_progress");
    expect(a.restore).toBe(true);
    // Never Done: archiving recorded nothing about whether the work finished.
    expect(a.status).not.toBe("done");
  });

  it("offers nothing on a task with no session to triage", () => {
    // `pending:<entry>` — triage is an overlay on triage.json keyed by SESSION
    // id, and a task that has never run has none. A button here would be a
    // button that can only fail.
    expect(archiveIntent(task({ key: "pending:e1", session_id: "", status: "upcoming" })))
      .toBe(null);
    // ...even when it is otherwise draggable, because run-now needs no session.
    const fresh = upcoming([T9], { key: "pending:e1", session_id: "" });
    expect(isDraggable(fresh)).toBe(true);
    expect(archiveIntent(fresh)).toBe(null);
  });

  it("is offered from every lane a session-bearing task can sit in", () => {
    for (const status of ["upcoming", "in_progress", "done", "archived"] as const) {
      expect(archiveIntent(task({ status }))).not.toBe(null);
    }
    // Including the one lane Done is refused from — filing a failed run away is
    // exactly what a person wants to do with it.
    expect(archiveIntent(task({ status: FAILED }))!.status).toBe("archived");
  });

  it("never disagrees with the drop the Board already makes", () => {
    const shapes: Task[] = [
      task({ status: "done" }),
      task({ status: "in_progress" }),
      task({ status: "archived" }),
      task({ status: FAILED }),
      upcoming([T9]),
      upcoming([T18, T9], { status: FAILED }),
      // The two that offer nothing.
      task({ key: "pending:e1", session_id: "", status: "upcoming" }),
      upcoming([T9], { key: "pending:e1", session_id: "" }),
    ];
    for (const t of shapes) {
      const a = archiveIntent(t);
      for (const col of BOARD_COLUMNS) {
        const action = dropAction(t, col.key);
        if (a && col.key === a.lane) {
          // The button makes the drop's call, on the drop's own lane.
          expect(action).toEqual({ kind: "triage", status: a.status });
          expect(dropLanes(t)).toContain(a.lane);
        } else if (!a) {
          // Offering nothing is only correct while the drag has no filing move
          // either — a run-now drop is a different verb and may still be legal.
          expect(action === null || action.kind === "run").toBe(true);
        }
      }
    }
  });
});

// ---- where the marks are drawn -------------------------------------------------
// Three claims the pure half cannot hold on its own: WHICH END of a row the
// unread count is at, whether the task row's ring and its thread's dots are one
// column, and what each mark is painted in. All three were the bug at some point,
// so all three are read out of the source rather than left to a screenshot.

const SHELL = import.meta.dir;
const VIEWS = readFileSync(join(SHELL, "ScheduleTaskViews.tsx"), "utf8");
/** The pure half's own source, for the handful of claims that are about HOW a rule
 *  is decided (which field it reads) rather than what it answers. */
const LIB = readFileSync(join(SHELL, "tasks-lib.ts"), "utf8");
const TASKS_CSS = readFileSync(join(SHELL, "../styles/tasks.css"), "utf8");
const SCHEDULE_CSS = readFileSync(join(SHELL, "../styles/schedule.css"), "utf8");
const TOKENS_CSS = readFileSync(join(SHELL, "../styles/tokens.css"), "utf8");
const REDUCED_MOTION_CSS = readFileSync(
  join(SHELL, "../styles/reduced-motion.css"),
  "utf8",
);

describe("the unread mark's position", () => {
  it("is a DOT on both views, and no number anywhere", () => {
    // Akshil, 2026-08-17: "only show a single dot like the notification that we
    // show". The pill and its whole vocabulary are gone — the component, the
    // class and the cap — rather than left orphaned behind a dot that replaced
    // them. The stylesheet is read stripped of comments, because the pill's own
    // history is deliberately still written down beside the mark that replaced it.
    expect(VIEWS).toContain("function UnreadDot(");
    expect(VIEWS).not.toContain("UnreadPill");
    expect(VIEWS).not.toContain("tasks-count");
    expect(TASKS_CSS.replace(/\/\*[\s\S]*?\*\//g, "")).not.toContain("tasks-count");
    expect(SCHEDULE_CSS.replace(/\/\*[\s\S]*?\*\//g, "")).not.toContain("tasks-count");
    // The same mark a message row wears, so the task and its messages stop being
    // two vocabularies for one fact.
    expect(VIEWS).toMatch(
      /function UnreadDot\([\s\S]*?className="tasks-dot"[\s\S]*?\n\}/,
    );
    // The count is not lost, it is unprinted: it is the mark's accessible name.
    expect(VIEWS).toMatch(/function UnreadDot\([\s\S]*?taskUnreadLabel\(count\)/);
    expect(VIEWS).toMatch(/function UnreadDot\([\s\S]*?aria-label=\{label\}/);
  });

  it("trails the TASK row's title instead of leading the row", () => {
    const from = VIEWS.indexOf('className={"tasks-row"');
    expect(from).toBeGreaterThan(-1);
    // The row ends where the thread it can open begins.
    const row = VIEWS.slice(from, VIEWS.indexOf("{open && (", from));
    const pill = row.indexOf("<UnreadDot");
    expect(pill).toBeGreaterThan(-1);
    // AFTER the title — that is the whole point of this round: the title is what
    // the list is scanned for, so it must be the first thing the row says.
    expect(pill).toBeGreaterThan(row.indexOf('className="tasks-title"'));
    // ...and therefore after the ring and the id too.
    expect(pill).toBeGreaterThan(row.indexOf("<StatusIcon"));
    expect(pill).toBeGreaterThan(row.indexOf("<IdChip"));
    // The leading rail slot is gone from the task row: the ring stands in that
    // column now, and there is no second component reserving it.
    expect(row).not.toContain("<UnreadRail");
    expect(VIEWS).not.toContain("function UnreadRail");
  });

  it("sits INSIDE the board card's title, after the last word, on any number of lines", () => {
    const card = VIEWS.slice(
      VIEWS.indexOf('<span className="schedule-tv-card-head">'),
      VIEWS.indexOf('<span className="schedule-tv-card-foot">'),
    );
    // Not in the head — the head is the card's id, its live ping and (only when it
    // disagrees with the lane) a status ring.
    const headOnly = card.slice(0, card.indexOf('className="schedule-tv-card-title"'));
    expect(headOnly).toContain("<IdChip");
    expect(headOnly).not.toContain("<UnreadDot");
    // Inside the title element, and after the words: the mark is a sibling of the
    // TEXT, in the same inline flow, which is the only arrangement that puts it
    // after the last word of a title that wraps — on the second line of a two-line
    // title, it stays on that line rather than dropping under it.
    expect(card).toMatch(
      /className="schedule-tv-card-title">\s*\{firstLine\(task\.title\)[^}]*\}\s*<UnreadDot count=\{unread\} \/>\s*<\/span>/,
    );
    // The wrapper that used to hold the two as flex siblings is GONE, along with
    // both of its bugs. It existed because the title clipped its overflow, so a
    // pill inside was eaten by a long title; out here it wrapped instead, which
    // orphaned it on a line of its own under any two-line title (the screenshot,
    // Akshil 2026-08-17). Neither the element nor its rule may come back — only
    // the note explaining why, which is why the stylesheet is read stripped.
    expect(VIEWS).not.toContain('className="schedule-tv-card-name"');
    expect(SCHEDULE_CSS.replace(/\/\*[\s\S]*?\*\//g, "")).not.toContain(
      "schedule-tv-card-name",
    );
  });

  it("cannot be clipped, because the card's title no longer clamps at all", () => {
    // The clamp was the cause of both earlier failures: nothing can flow after the
    // last word of a clipped box AND stay outside the clip, so it had to go rather
    // than be worked around a third time. A title now takes as many lines as it
    // needs and the card grows — accepted, and rare, because a card's title is the
    // session's short name and not the message it sends.
    const title = SCHEDULE_CSS.slice(
      SCHEDULE_CSS.indexOf(".schedule-tv-card-title {"),
      SCHEDULE_CSS.indexOf("}", SCHEDULE_CSS.indexOf(".schedule-tv-card-title {")),
    );
    expect(title).toBeTruthy();
    expect(title).not.toContain("line-clamp");
    expect(title).not.toContain("-webkit-box");
    expect(title).not.toContain("overflow: hidden");
    expect(title).not.toContain("text-overflow");
    expect(title).not.toContain("white-space");
    // Nothing between the pill and the card hides overflow either — a clip one
    // level out would lose the pill exactly as the clamp did. The chain is the
    // title, the card, and the wrapper the action strip is pinned against.
    for (const rule of [
      /\.schedule-tv-board \.schedule-tv-card,[\s\S]*?\n\}/,
      /\.tasks-card-wrap \{[\s\S]*?\n\}/,
    ] as const) {
      const src = rule.source.includes("card-wrap") ? TASKS_CSS : SCHEDULE_CSS;
      const block = src.match(rule)?.[0];
      expect(block).toBeTruthy();
      expect(block).not.toContain("overflow");
    }
    // The one guard kept from the clamped version, and now the only thing standing
    // between a 200-character unbroken token and a blown-out 260px column.
    expect(title).toContain("overflow-wrap: anywhere");
    // What makes it read as part of the line rather than as a box hanging off the
    // baseline. `inline-block` comes from `.tasks-dot` itself, shared with the row.
    expect(SCHEDULE_CSS).toMatch(
      /\.schedule-tv-card-title \.tasks-dot\s*\{[^}]*vertical-align: middle/,
    );
    expect(TASKS_CSS).toMatch(/\.tasks-dot\s*\{[^}]*display: inline-block/);
  });

  it("keeps the row's ONE flex spacer and adds no auto margin", () => {
    // Free space is split equally between every `auto` margin, so a second one
    // centres the right-hand group instead of pushing it to the end. Neither mark
    // may smuggle one in as it crosses the row: the separation comes from the row's
    // `gap`, and `.tasks-grow` stays the only spacer on both kinds of row.
    expect((TASKS_CSS.match(/margin-left: auto/g) ?? []).length).toBe(0);
    expect(TASKS_CSS).toMatch(/\.tasks-grow\s*\{[^}]*flex: 1 1 auto/);
    expect(TASKS_CSS).toMatch(/\.tasks-row\s*\{[^}]*gap: var\(--tasks-row-gap\)/);
    expect(TASKS_CSS).toMatch(/\.tasks-msg\s*\{[^}]*gap: var\(--tasks-row-gap\)/);
    expect(TASKS_CSS).toMatch(/\.tasks-dot\s*\{[^}]*flex: 0 0 7px/);
    expect(TASKS_CSS).not.toMatch(/\.tasks-dot\s*\{[^}]*margin/);
    // The board card is the one place the mark carries a margin, and it is a FIXED
    // one: inside the title's text flow there is no flex parent to provide a `gap`,
    // and `auto` has no meaning in inline layout anyway. It must not leak onto the
    // List row's mark, which takes its spacing from the row's own `gap` — hence the
    // rule is scoped to the card's title.
    expect(SCHEDULE_CSS).toMatch(
      /\.schedule-tv-card-title \.tasks-dot\s*\{[^}]*margin-left: 6px/,
    );
    // ...and the row's own trailing time carries none either, for the same reason.
    expect(TASKS_CSS).not.toMatch(/\.tasks-row-time\s*\{[^}]*margin/);
  });

  it("trails every MESSAGE row's title too, in the same order as the task row", () => {
    // The dot used to LEAD its row, in a reserved slot on the rail, and that was
    // the count's own bug one indent in: the mark announced before the words it is
    // about. Worse, the task row above had already been fixed, so the thread read
    // as a different dialect of the same page (Akshil, 2026-08-17). Both halves of
    // unread now trail their titles; only the position moved.
    const thread = VIEWS.slice(VIEWS.indexOf('className={"tasks-msg"'));
    const body = thread.indexOf('className="tasks-msg-body"');
    const dot = thread.indexOf('className="tasks-dot"');
    expect(body).toBeGreaterThan(-1);
    expect(dot).toBeGreaterThan(body);
    // ...and therefore after the ring, the kind glyph and the id too.
    expect(dot).toBeGreaterThan(thread.indexOf("<StatusIcon"));
    expect(dot).toBeGreaterThan(thread.indexOf('className="tasks-msg-kind"'));
    expect(dot).toBeGreaterThan(thread.indexOf("<IdChip"));
    // Before the spacer, so it hugs the title rather than joining the trailing
    // metadata group — the same placement the task row's count has.
    expect(dot).toBeLessThan(thread.indexOf('className="tasks-grow"'));
    // The reserved head slot is gone with it: nothing renders `.tasks-rail` and the
    // rule itself is no longer in the stylesheet.
    expect(VIEWS).not.toContain('className="tasks-rail"');
    expect(TASKS_CSS.replace(/\/\*[\s\S]*?\*\//g, "")).not.toContain(".tasks-rail");
    expect(VIEWS).not.toContain("tasks-msg-flag");
    // A SIBLING of the title, never inside it: the body ellipsises its overflow,
    // so a dot in there would vanish on exactly the longest lines.
    expect(thread).toMatch(
      /className="tasks-msg-body">\{firstLine\(m\.body\)[^}]*\}<\/span>/,
    );
  });

  it("derives every indent from the rail rather than typing it twice", () => {
    // The rail is placed once (--tasks-rail-x): it is where a TASK row's ring
    // stands. A MESSAGE row's ring stands one ring slot and one gap to its right,
    // and THAT is derived too, then the thread's padding is derived from it by
    // subtracting a message row's own left padding. Hand-tune any of the three
    // separately and the columns part company, which is the bug this geometry
    // exists to prevent.
    expect(TASKS_CSS).toContain("--tasks-rail-x: calc(");
    expect(TASKS_CSS).toMatch(
      /--tasks-msg-ring-x: calc\(\s*var\(--tasks-rail-x\) \+ var\(--tasks-rail-w\) \+ var\(--tasks-row-gap\)\s*\);/,
    );
    expect(TASKS_CSS).toContain(
      "calc(var(--tasks-msg-ring-x) - var(--tasks-msg-indent))",
    );
    // The slot the derivation is made of is the RING's width. This is the whole
    // reason the offset survived the dot leaving the head: the empty slot used to
    // push the thread's rings into their column, so its width had to be written
    // down before it could be deleted, or every message row would have slid left
    // into the task row's own column.
    expect(TASKS_CSS).toMatch(/--tasks-rail-w: 16px/);
    expect(SCHEDULE_CSS).toMatch(/\.schedule-ring\s*\{[^}]*flex: 0 0 16px/);
  });

  it("draws no vertical rule down the thread", () => {
    // The indent is the whole signal. A rule beside it read as a stray line
    // sitting in front of the indentation rather than as a guide belonging to
    // it, and nothing else in this list is fenced. Pinned because the geometry
    // above depends on it: the message row's left padding was written as
    // indent-minus-1 to make room for the rule's own width, so re-adding the
    // border without re-doing the subtraction would shift every message row a
    // pixel off the column it is supposed to share with the task above.
    const body = TASKS_CSS.replace(/\/\*[\s\S]*?\*\//g, "");
    expect(body).not.toContain("border-left");
    expect(body).toMatch(/\.tasks-msg\s*\{[^}]*padding: 5px 8px 5px var\(--tasks-msg-indent\)/);
  });
});

// ---- the status vocabulary's five hues ------------------------------------------
// Upcoming · In Progress · Done · Failed · Archive. Read out of tokens.css because
// the whole vocabulary only works if the five stay DISTINCT in both themes, and
// two of them were swapped on 2026-08-17 (Upcoming to grey, Archive off grey to a
// muted violet — and then, the same day, off that violet to a dusty rose, because
// at ring size a low-chroma violet beside a grey is two greys).
//
// A colour test cannot judge taste. What it can pin is that the five are five, that
// neither theme was left behind, that the two that moved did not move onto each
// other or onto a neighbour — and, since the violet cleared every one of those and
// still failed, that Archive is far enough from Upcoming in CHROMA to be told apart
// at a glance rather than merely under a picker.

/** The `--status-*` and `--activity` values of one palette block. */
function statusHues(selector: string): Record<string, string> {
  const at = TOKENS_CSS.indexOf(selector + " {");
  expect(at).toBeGreaterThan(-1);
  const block = TOKENS_CSS.slice(at, TOKENS_CSS.indexOf("\n}", at));
  const out: Record<string, string> = {};
  for (const [, name, value] of block.matchAll(
    /--(status-[a-z_]+|activity):\s*([^;]+);/g,
  )) {
    out[name] = value.trim();
  }
  return out;
}

/** Any single token's value out of one palette block — for the ones Upcoming
 *  borrows rather than restates (`var(--fg-muted)`), which have to be RESOLVED
 *  before two colours can be compared as colours. */
function paletteHex(selector: string, token: string): string {
  const at = TOKENS_CSS.indexOf(selector + " {");
  expect(at).toBeGreaterThan(-1);
  const block = TOKENS_CSS.slice(at, TOKENS_CSS.indexOf("\n}", at));
  const found = block.match(new RegExp(`--${token}:\\s*(#[0-9a-f]{6});`));
  expect(found).toBeTruthy();
  return found![1];
}

/** `#rrggbb` as three 0-255 numbers, so a claim about a hue can be arithmetic
 *  over the actual channels rather than a string comparison — two colours that
 *  differ by one bit are different strings and the same colour. */
function channels(hex: string): number[] {
  expect(hex).toMatch(/^#[0-9a-f]{6}$/);
  return [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16));
}

// ---- where the status ring is drawn, and where it is not -----------------------
// The ring is the page's status vocabulary, so the question is not whether it is
// good but whether each place it appears is SAYING something there. On a board card
// it usually was not: the lane header states the lane, and every card in that lane
// then said it again next to its id (Akshil, 2026-08-17 — "just repetitive here").
//
// But "repetitive" is a claim about AGREEMENT, and `failed` is a flag beside
// `status` rather than a value of it, so the two can disagree — a broken run triaged
// to Done, or live again (server routers/tasks.py `_failed`). On those cards the
// ring was the only at-rest mark saying the run broke, and deleting it outright lost
// a signal rather than a repetition. So the rule is conditional, and it is pinned
// from both ends: the RULE as logic over `isFailedTask` and `taskColumn`, which is
// what the card asks, and the CALL SITE as source, because "only when it disagrees
// with the lane" is a claim about markup no unit of pure logic can hold.

describe("the board card's status ring", () => {
  /** The card's markup, from its head down to the action strip beside it. */
  const CARD = VIEWS.slice(
    VIEWS.indexOf('<span className="schedule-tv-card-head">'),
    VIEWS.indexOf('className="tasks-card-acts"'),
  );

  /** Exactly what the card asks: does the ring say anything the lane has not? */
  const saysSomething = (t: Task) => isFailedTask(t) && taskColumn(t) !== "failed";

  it("says nothing extra on a card whose status IS its lane", () => {
    // The common card, and the whole of what was asked for. Every lane, including
    // Failed itself — a failed card in the Failed lane is the agreement case, and
    // its header has already said the word.
    for (const status of ["upcoming", "in_progress", "done", "archived"] as const) {
      expect(saysSomething(task({ status }))).toBe(false);
    }
    expect(saysSomething(task({ status: "failed", failed: true }))).toBe(false);
    // A lane the client does not recognise is filed under Done (taskColumn), and it
    // agrees with the lane it was filed into, so it draws nothing either.
    expect(saysSomething(task({ status: "invented-later" as Task["status"] }))).toBe(false);
  });

  it("draws on a failed task filed somewhere other than Failed", () => {
    // The two directions `_failed` documents: a broken run the user triaged away,
    // and one whose session is live again. Both sit under a header that says nothing
    // about the failure, so the ring is the card's only at-rest tell — the Re-send
    // button is hover-revealed, and a control is not a signal.
    expect(saysSomething(task({ status: "done", failed: true }))).toBe(true);
    expect(saysSomething(task({ status: "in_progress", failed: true }))).toBe(true);
    expect(saysSomething(task({ status: "archived", failed: true }))).toBe(true);
  });

  it("asks that rule at the call site, through the one helper that knows it", () => {
    // `isFailedTask` is the single notion of "reads as failed" (the failed lane, or
    // the flag that repaints a Done ring red). A second inline reading of
    // `task.failed` here is how the card and the List row would drift apart.
    expect(CARD).toContain("{failedOffLane && <StatusIcon status={lane} failed />}");
    expect(CARD).toContain("<IdChip");
    const card = VIEWS.slice(VIEWS.indexOf("function TaskCard("));
    expect(card).toContain('isFailedTask(task) && lane !== "failed"');
    expect(card).toContain("const lane = taskColumn(task)");
  });

  it("changes nothing about the ring itself — same component, hue and size", () => {
    // Conditional, not restyled: a second failure marker with its own look would be
    // a new word in a vocabulary this round was pruning.
    expect(CARD).not.toContain("schedule-ring");
    expect(SCHEDULE_CSS).toContain(".schedule-ring--failed,");
    expect(SCHEDULE_CSS).toContain("color: var(--status-failed);");
    expect(SCHEDULE_CSS).toMatch(/\.schedule-ring\s*\{[^}]*flex: 0 0 16px/);
  });

  it("stays on the lane header, which is the one place it always says something", () => {
    // Both forms of the header: the open lane, and the collapsed rail it becomes.
    for (const cls of ["schedule-tv-lane-head", "schedule-tv-rail"]) {
      const at = VIEWS.indexOf(`"${cls}"`);
      expect(at).toBeGreaterThan(-1);
      expect(VIEWS.slice(at, VIEWS.indexOf("</button>", at))).toContain(
        "<StatusIcon status={col.key} />",
      );
    }
  });

  it("stays UNconditional on a List row and in the Calendar, which have no lane", () => {
    const from = VIEWS.indexOf('className={"tasks-row"');
    const row = VIEWS.slice(from, VIEWS.indexOf("{open && (", from));
    // Not gated on anything: a flat row and a day cell have no header above them, so
    // the ring is the only thing that files them at all.
    expect(row).toContain("<StatusIcon status={taskColumn(task)} failed={task.failed} />");
    expect(VIEWS.slice(VIEWS.indexOf('className={"tasks-msg"'))).toContain("<StatusIcon");
    expect(SCHEDULE_CSS).toContain(".schedule-cal-popover .schedule-ring");
  });

  it("holds the head's line whether or not a ring is standing in it", () => {
    // Two failures this prevents. The strip is pinned over the head and centred on
    // it, and the title starts one card `gap` below — clearance that came free while
    // EVERY head held a 16px ring; a ringless head is one 11px id chip and the
    // strip's opaque buttons reach over the title's first line on hover. And with
    // the ring conditional, a head left to its contents would stand 16px on a
    // failed-off-lane card and 13px on its neighbour, so a lane would jitter by the
    // width of a glyph. One stated line answers both, and it is the ring's own
    // height, so the two cases are exactly the same size.
    expect(TASKS_CSS).toMatch(/--tasks-card-head-h: 16px/);
    expect(TASKS_CSS).toMatch(
      /\.tasks-card-wrap \.schedule-tv-card-head\s*\{[^}]*min-height: var\(--tasks-card-head-h\)/,
    );
    expect(SCHEDULE_CSS).toMatch(/\.schedule-ring\s*\{[^}]*height: 16px/);
  });
});

/** A rule's declarations, found by WHOLE selector rather than by substring: many
 *  rules on this page are written twice over (`.x` and `.prefs-section .x`, because
 *  `.prefs-section button` repaints unarmored buttons here), and half of them are
 *  named in the prose above themselves. Comments are stripped first so a mention
 *  cannot be mistaken for the rule. */
function block(css: string, selector: string): string {
  for (const rule of rules(css)) {
    if (rule.selectors.includes(selector)) return rule.body;
  }
  throw new Error(`no rule whose selector list holds exactly "${selector}"`);
}

/** Every rule in a stylesheet, as its selector list and its declarations. A
 *  selector can appear in more than one rule (a resting rule and a state rule),
 *  which `block` above cannot express — it answers with the first. */
function rules(css: string): { selectors: string[]; body: string }[] {
  const bare = css.replace(/\/\*[\s\S]*?\*\//g, "");
  return [...bare.matchAll(/([^{}]+)\{([^{}]*)\}/g)].map(([, list, body]) => ({
    selectors: list.split(",").map((s) => s.trim()),
    body,
  }));
}

// ---- which thing on the board is a surface -------------------------------------
// The lane used to paint a fill at rest, so an empty lane was a grey slab and every
// card competed with the box around it (Akshil, 2026-08-17, against flow side by
// side). The fill is gone and the CARD is the only surface — but the lane is still
// BOUNDED, by the same hairline the collapsed rail wears, because unfilled and
// unbounded a column had no edge of its own at all (Akshil, same day: "at least
// they should have an outline when they're expanded"). Four things then have to stay
// true, and each was nearly broken by one of those two changes, so all four are read
// out of the stylesheets: no fill comes back, the edge is there and is the rail's
// edge, the card still lifts off the PAGE in both themes, and a drop target still
// announces itself on a box that paints almost nothing — without its dashed line
// stacking onto the hairline that is now underneath it.

describe("the board's surfaces", () => {
  it("gives the lane no resting fill, but does give it an edge", () => {
    const lane = block(SCHEDULE_CSS, ".schedule-tv-lane-body");
    // No FILL: a filled lane is a surface competing with the cards on it, which is
    // the whole reason the plate came off.
    expect(lane).not.toContain("background");
    expect(block(SCHEDULE_CSS, ".schedule-tv-lane")).not.toContain("background");
    // But BOUNDED — "at least they should have an outline when they're expanded".
    // Unfilled and unbounded, the column's only edge was the widest card's ragged
    // right-hand end.
    expect(lane).toContain("border: 1px solid var(--border)");
    expect(lane).toContain("border-radius: 8px");
    // Stated with it, so `min-height: 120px` stays the floor the board actually has.
    expect(lane).toContain("box-sizing: border-box");
    // One hairline all the way round, not a rule down one side.
    expect(lane).not.toContain("border-left");
    expect(lane).not.toContain("border-right");
    // The collapsed lane is a lane too, and since 2026-08-17 the two wear the SAME
    // edge — same width, same token, same radius, both unfilled — so the open and
    // closed forms read as one family rather than as two ideas about a lane's edge.
    const rail = block(SCHEDULE_CSS, ".schedule-tv-rail");
    expect(rail).toContain("background: transparent");
    expect(rail).toContain("border: 1px solid var(--border)");
    expect(rail).toContain("border-radius: 8px");
  });

  it("makes the card lift off the PAGE, in both themes, from tokens", () => {
    const card = block(SCHEDULE_CSS, ".schedule-tv-board .schedule-tv-card");
    // `--bg` was the old fill and is also what the page ground is painted in
    // (base.css `body`), so keeping it would have made the card vanish against the
    // page — most obviously in light mode, where both are plain white.
    expect(card).not.toMatch(/background: var\(--bg\);/);
    // `--bg-panel` is the app's "surface floating above the page" token: it steps
    // lighter than the ground in dark and stays white in light, where the hairline
    // and the shadow do the lifting instead.
    expect(card).toContain("background: var(--bg-panel)");
    expect(card).toContain("border: 1px solid var(--border)");
    expect(card).toContain("box-shadow: 0 1px 2px var(--shadow-sm)");
    // Both halves of the lift are theme-tuned tokens with a value in BOTH blocks,
    // never a colour defined only under `[data-theme]` — a light-only definition is
    // how one theme ends up with no card surface at all.
    for (const token of ["--bg-panel", "--shadow-sm"]) {
      expect(TOKENS_CSS.slice(0, TOKENS_CSS.indexOf(':root[data-theme="light"]'))).toContain(
        `${token}:`,
      );
      expect(TOKENS_CSS.slice(TOKENS_CSS.indexOf(':root[data-theme="light"]'))).toContain(
        `${token}:`,
      );
    }
    // The hover-revealed action strip is painted in the card's surface so it reads
    // as floating over the head; it has to track the card or it is a patch on it.
    expect(block(TASKS_CSS, ".tasks-card-act")).toContain("background: var(--bg-panel)");
  });

  it("lets a drop target announce itself by ADDING paint, not by changing it", () => {
    // This is why removing the resting fill cost the drag nothing: every drop state
    // goes from nothing to something. A legal lane outlines dashed the moment a drag
    // starts, the lane under the pointer takes the accent wash, and the one drop
    // that SENDS rather than files swaps the hue for `--activity` and says so in
    // words. On a transparent lane all four are more legible than they were on a
    // tinted one, not less.
    expect(SCHEDULE_CSS).toMatch(
      /\.schedule-tv-lane-body\.is-drop-legal\s*\{[^}]*outline: 1px dashed color-mix\(in srgb, var\(--accent\)/,
    );
    expect(SCHEDULE_CSS).toMatch(
      /\.schedule-tv-lane-body\.is-drop-over,[\s\S]*?background: color-mix\(in srgb, var\(--accent\) 14%/,
    );
    expect(SCHEDULE_CSS).toMatch(
      /\.schedule-tv-rail\.is-drop-legal,[\s\S]*?outline: 1px dashed color-mix\(in srgb, var\(--accent\)/,
    );
    expect(TASKS_CSS).toMatch(
      /\.schedule-tv-lane-body\.is-drop-run,[\s\S]*?outline: 1px dashed color-mix\(in srgb, var\(--activity\)/,
    );
    expect(TASKS_CSS).toMatch(/\.tasks-run-hint\s*\{[^}]*background: color-mix/);
    // The lane keeps the geometry those states are drawn with — the padding that
    // insets the outline off the top card, and the radius the wash takes.
    const lane = block(SCHEDULE_CSS, ".schedule-tv-lane-body");
    expect(lane).toContain("padding: 4px");
    expect(lane).toContain("border-radius: 8px");
    // And it keeps a floor, so an EMPTY lane — which now shows its header and
    // nothing else, because "nothing scheduled" should look like nothing — is still
    // something a card can be dragged into.
    expect(lane).toContain("min-height: 120px");
    // All three classes are still applied to both forms of the lane.
    for (const state of ["is-drop-legal", "is-drop-over", "is-drop-run"]) {
      expect((VIEWS.match(new RegExp(`" ${state}"`, "g")) ?? []).length).toBe(2);
    }
  });

  it("does not let the dashed drop line double up on the lane's own hairline", () => {
    // The outline is drawn at `outline-offset: -1px`, which since the lane grew a
    // border is exactly where that border is: left alone, the two stack into one
    // muddy 2px edge, half grey and half accent, which reads as "thicker" rather
    // than as "here". So a lane being dropped into takes its hairline to transparent
    // and the dashed line stands in its place — one line, same position, same
    // weight, state carried by colour and dashes.
    // ONE rule does it, and its selector list has to hold all six forms of "a lane
    // being dropped into" — both drop hues, and both the open lane and the rail.
    const swap = rules(SCHEDULE_CSS).find(
      (r) =>
        r.body.includes("border-color: transparent") &&
        r.selectors.includes(".schedule-tv-lane-body.is-drop-legal"),
    );
    expect(swap).toBeTruthy();
    for (const selector of [
      ".schedule-tv-lane-body.is-drop-legal",
      ".schedule-tv-lane-body.is-drop-run",
      ".schedule-tv-rail.is-drop-legal:not(:disabled)",
      ".schedule-tv-rail.is-drop-run:not(:disabled)",
      ".prefs-section .schedule-tv-rail.is-drop-legal:not(:disabled)",
      ".prefs-section .schedule-tv-rail.is-drop-run:not(:disabled)",
    ]) {
      expect(swap!.selectors).toContain(selector);
    }
    // The border-WIDTH never changes, so nothing shifts by a pixel when a drag
    // starts: the box the cards sit in is the size it was.
    expect(swap!.body).not.toContain("border-width");
    expect(swap!.body).not.toContain("border:");
    // And the rail's drop states have to OUTRANK its hover rule, which restates
    // `border-color` because `.prefs-section button:hover` would otherwise paint the
    // edge accent. Same specificity, so the swap has to come later in the file — or
    // the hairline returns on exactly the rail the pointer is over.
    const hover = SCHEDULE_CSS.indexOf(
      ".prefs-section .schedule-tv-rail:hover:not(:disabled)",
    );
    const dropped = SCHEDULE_CSS.indexOf(
      ".prefs-section .schedule-tv-rail.is-drop-legal:not(:disabled)",
    );
    expect(hover).toBeGreaterThan(-1);
    expect(dropped).toBeGreaterThan(hover);
    expect(block(SCHEDULE_CSS, ".prefs-section .schedule-tv-rail:hover:not(:disabled)")).toContain(
      "border-color: var(--border)",
    );
  });

  it("gives the card the reference's breathing room, and the strip follows it", () => {
    const card = block(SCHEDULE_CSS, ".schedule-tv-board .schedule-tv-card");
    // Looser than the 8px/10px and 5px it shipped with, which is what made three
    // short lines feel crowded. On the repo's even scale, and the three lines keep
    // their relative hierarchy — no type size moved.
    expect(card).toContain("padding: 10px 12px");
    expect(card).toContain("gap: 6px");
    expect(block(SCHEDULE_CSS, ".schedule-tv-lane-body")).toContain("gap: 8px");
    // The action strip is centred on the head off the card's TOP PADDING (plus half
    // the head's line, less half a 22px button), so loosening the card without
    // moving this leaves the strip riding high over the head: 10 + 8 - 11 = 7.
    expect(TASKS_CSS).toMatch(/\.tasks-card-acts\s*\{[^}]*top: 7px/);
  });
});

describe("the status ring's five hues", () => {
  const LANES = [
    "status-upcoming",
    "status-progress",
    "status-done",
    "status-failed",
    "status-archived",
  ];

  for (const [theme, selector] of [
    ["dark", ":root"],
    ["light", ':root[data-theme="light"]'],
  ] as const) {
    it(`gives ${theme} all five lanes, and five different values`, () => {
      const hues = statusHues(selector);
      for (const lane of LANES) expect(hues[lane]).toBeTruthy();
      // Five lanes, five hues. A duplicate is a vocabulary with a word missing.
      expect(new Set(LANES.map((l) => hues[l])).size).toBe(5);
    });

    it(`paints ${theme}'s Upcoming with an existing neutral, not a hue`, () => {
      // Blue was the loudest thing on a page about what is happening NOW, so
      // Upcoming recedes into the row's own metadata colour. It borrows the token
      // rather than restating a grey, which is what keeps the two in step.
      expect(statusHues(selector)["status-upcoming"]).toBe("var(--fg-muted)");
    });

    it(`keeps ${theme}'s Archive off grey and off every live lane`, () => {
      const hues = statusHues(selector);
      const archive = hues["status-archived"];
      // Not the grey Upcoming took, and not the blue either.
      expect(archive).not.toBe(hues["status-upcoming"]);
      expect(archive).not.toBe(hues["activity"]);
      // A literal, and a ROSE: red leads, and blue stands well clear of green.
      expect(archive).toMatch(/^#[0-9a-f]{6}$/);
      const [r, g, b] = channels(archive);
      expect(r).toBeGreaterThan(g);
      expect(r).toBeGreaterThan(b);
      // The blue-over-green margin is what separates a rose from a RED: on
      // --status-failed those two channels are equal, so a warm hue with b == g is
      // the failed lane wearing a different lightness. It is also what keeps it off
      // the dull bronze that was tried and lost, which reads as a dimmer In
      // Progress for exactly the opposite reason (b below g).
      expect(b - g).toBeGreaterThanOrEqual(25);
      // Low-saturation, so a filed lane never out-shouts a live one: the spread
      // between the strongest and weakest channel stays modest.
      expect(Math.max(r, g, b) - Math.min(r, g, b)).toBeLessThan(90);
    });

    it(`makes ${theme}'s Archive read as a colour beside Upcoming's grey`, () => {
      // The one thing the old muted violet got wrong, and the reason this test is
      // about CHANNELS rather than about the two values differing: #a898cb was a
      // different string from --fg-muted and still read as barely-tinted grey at
      // ring size — "quite similar" (Akshil, 2026-08-17). Distinguishable means the
      // two are far apart in CHROMA, so a reader can tell them apart at a glance and
      // not merely under a colour picker.
      const hues = statusHues(selector);
      // Upcoming is `var(--fg-muted)`, so resolve it: the grey it borrows is the
      // thing Archive has to be told apart from, not the token's spelling.
      const grey = channels(paletteHex(selector, "fg-muted"));
      const archive = channels(hues["status-archived"]);
      const chroma = (c: number[]) => Math.max(...c) - Math.min(...c);
      // A near-neutral, as a grey must be — this is what Archive is up against.
      expect(chroma(grey)).toBeLessThanOrEqual(15);
      // ...and Archive carries real chroma, several times the grey's. The old violet
      // cleared this by hue but not by margin, which is why the margin is the pin.
      expect(chroma(archive)).toBeGreaterThanOrEqual(40);
      expect(chroma(archive) - chroma(grey)).toBeGreaterThanOrEqual(30);
      // And it is WARM: at this saturation a cool hue collapses into "cool grey",
      // which is the mistake being corrected. Red leads on the rose and does not on
      // the grey (whose channels only ever climb towards blue).
      expect(archive.indexOf(Math.max(...archive))).toBe(0);
      expect(grey.indexOf(Math.max(...grey))).toBe(2);
    });
  }

  it("wears each lane's token on the ring, and nothing hardcoded", () => {
    for (const [lane, token] of [
      ["upcoming", "--status-upcoming"],
      ["in_progress", "--status-progress"],
      ["done", "--status-done"],
      ["archived", "--status-archived"],
    ] as const) {
      expect(SCHEDULE_CSS).toContain(
        `.schedule-ring--${lane} { color: var(${token}); }`,
      );
    }
    expect(SCHEDULE_CSS).toContain("color: var(--status-failed);");
  });

  it("keeps the surviving blue on the things that DO something", () => {
    // The blue was never the Upcoming hue; it was the page's activity hue, and
    // naming it that is what let Upcoming go grey without repainting the rest. What
    // it still owns is movement: the live ping, the Run now affordance and the
    // drag-to-run outline. The unread dot left this list on 2026-08-17 (see below)
    // and nothing else did.
    for (const theme of [":root", ':root[data-theme="light"]']) {
      expect(statusHues(theme)["activity"]).toMatch(/^#[0-9a-f]{6}$/);
    }
    expect(SCHEDULE_CSS).toMatch(
      /\.schedule-tv-pulse::before,\n\.schedule-tv-pulse::after \{[^}]*background: var\(--activity\)/,
    );
    expect(TASKS_CSS).toMatch(
      /\.tasks-act--run:hover:not\(:disabled\),\n\.prefs-section \.tasks-act--run:hover:not\(:disabled\) \{[^}]*color: var\(--activity\)/,
    );
    expect(TASKS_CSS).toMatch(
      /\.schedule-tv-lane-body\.is-drop-run,[\s\S]{0,160}outline: 1px dashed color-mix\(in srgb, var\(--activity\)/,
    );
    // Nothing paints with the old name any more except the ring it belongs to.
    expect(TASKS_CSS).not.toContain("var(--status-upcoming)");
  });

  it("paints the unread mark GREY, and never in the activity hue", () => {
    // Akshil, 2026-08-17: "a subtle grey dot instead of a blue dot". The dot wore
    // `--activity` while it spoke for one message at a time; once it also stood for
    // a whole task, a column of them read as a page full of alerts. Unread is news,
    // not an alarm — and it is the only thing in that slot, so it does not need a
    // hue to be found.
    const rule = block(TASKS_CSS, ".tasks-dot");
    expect(rule).toContain("background: var(--fg-muted)");
    expect(rule).not.toContain("--activity");
    // An EXISTING neutral, the one the row's own metadata speaks in: no new colour
    // entered the page for this.
    for (const theme of [":root", ':root[data-theme="light"]']) {
      expect(TOKENS_CSS.slice(TOKENS_CSS.indexOf(theme))).toContain("--fg-muted:");
    }
    // Quiet is the goal; invisible is not. Full-strength `--fg-muted` rather than a
    // wash of it — measured at 7.0:1 on the resting row and 5.6:1 on the hover fill
    // in dark, 5.9:1 and 4.7:1 in light, which is where the blue was.
    expect(rule).not.toContain("color-mix");
    expect(rule).not.toContain("opacity");
    // And nothing else re-tints it — not on the card, where it rides inside the
    // title, and not on the row.
    expect(SCHEDULE_CSS).not.toMatch(/\.schedule-tv-card-title \.tasks-dot\s*\{[^}]*background/);
    expect(TASKS_CSS).not.toMatch(/\.tasks-row \.tasks-dot\s*\{[^}]*background/);
  });

  it("shares its grey with the Upcoming ring, and says why that is not a clash", () => {
    // `--status-upcoming` is `--fg-muted` too, so an Upcoming row carries two grey
    // marks. They differ in every other way — a 16px HOLLOW ring in the left rail
    // against a 7px FILLED disc after the last word of the title, with the row's
    // width between them — and the stylesheet has to say so, or the next reader
    // "fixes" one of them.
    for (const theme of [":root", ':root[data-theme="light"]']) {
      expect(statusHues(theme)["status-upcoming"]).toBe("var(--fg-muted)");
    }
    const dot = block(TASKS_CSS, ".tasks-dot");
    const ring = block(SCHEDULE_CSS, ".schedule-ring");
    expect(dot).toContain("width: 7px");
    expect(ring).toContain("width: 16px");
    // Filled versus hollow is the ring's own border, not a colour.
    expect(ring).toContain("border:");
    expect(dot).not.toContain("border:");
    expect(TASKS_CSS).toContain("--status-upcoming` is also `--fg-muted`");
  });
});

describe("the In Progress ring", () => {
  it("does not blink", () => {
    // A lane is a COLUMN of these, so a 2s breathing loop on each made a full
    // board flicker; the ring is already the only yellow one on the page.
    expect(SCHEDULE_CSS).not.toMatch(/\.schedule-ring--in_progress\s*\{[^}]*animation/);
    expect(SCHEDULE_CSS).not.toContain(".schedule-ring--in_progress {\n    animation");
    // Static yellow is all it is.
    expect(SCHEDULE_CSS).toContain(
      ".schedule-ring--in_progress { color: var(--status-progress); }",
    );
  });

  it("leaves the live ping — and only the live ping — moving", () => {
    // The ping is a single mark on the handful of tasks with a turn actually in
    // flight, which is a fact about right now rather than about a lane. Its
    // keyframes therefore stay, and stay motion-gated.
    expect(SCHEDULE_CSS).toContain("@keyframes schedule-tv-pulse");
    const at = SCHEDULE_CSS.indexOf("@media (prefers-reduced-motion: no-preference) {");
    expect(at).toBeGreaterThan(-1);
    const guard = SCHEDULE_CSS.slice(at, SCHEDULE_CSS.indexOf("\n}", at));
    expect(guard).toContain(".schedule-tv-pulse::before");
    expect(guard).not.toContain("schedule-ring--in_progress");
  });

  it("leaves nothing dangling in reduced-motion.css", () => {
    // That file names the individual animations it has to override by hand; it
    // never named this one (the blanket duration rule covered it), so removing the
    // rule leaves no counterpart behind.
    expect(REDUCED_MOTION_CSS).not.toContain("schedule-ring");
    expect(REDUCED_MOTION_CSS).not.toContain("schedule-tv-pulse");
  });
});

// ---- the List row's disclosure -------------------------------------------------
// isExpandable decides whether a row is an accordion; these are the claims about
// MARKUP that no unit of pure logic can hold — that the chevron is behind that
// predicate, that the row keeps everything else it does, and above all that the
// glyph goes without the GUTTER going. `--tasks-caret-w` is the first term of
// `--tasks-rail-x`, which every indent on the page is derived from, so a chevron
// removed by deleting its element takes that row's status ring and the whole rail
// with it and turns a column of rings into a zigzag.

describe("the one-message row's missing chevron", () => {
  /** The task row's markup, head to thread. */
  const ROW = VIEWS.slice(
    VIEWS.indexOf('className={"tasks-row"'),
    VIEWS.indexOf("{open && (", VIEWS.indexOf('className={"tasks-row"')),
  );

  it("puts the glyph behind the predicate and the gutter in front of it", () => {
    // The SPAN is unconditional — no `{expandable && (` around it — so the gutter is
    // drawn on every row...
    expect(ROW).toContain('className={"tasks-caret" + (open ? " is-open" : "")}');
    expect(ROW).not.toMatch(/expandable &&\s*\(?\s*<span[\s\S]{0,40}?"tasks-caret"/);
    // ...and only its CONTENTS are conditional.
    expect(ROW).toContain("{expandable ? ICON_CHEVRON : null}");
    // The gutter's width is the element's own, not the glyph's, or an empty span
    // would collapse and undo the whole point.
    const caret = block(TASKS_CSS, ".tasks-caret");
    expect(caret).toContain("flex: 0 0 var(--tasks-caret-w)");
    expect(caret).toContain("width: var(--tasks-caret-w)");
    // And that is the term every indent is measured from, still.
    expect(TASKS_CSS).toContain("--tasks-caret-w: 16px");
    expect(TASKS_CSS).toMatch(
      /--tasks-rail-x: calc\(\s*var\(--tasks-row-pad\) \+ var\(--tasks-caret-w\) \+ var\(--tasks-row-gap\)/,
    );
  });

  it("makes the row unexpandable rather than only chevron-less", () => {
    // The guard is in the derived value, not in the render: a row in the List's
    // expanded set that stops being expandable closes, instead of being stuck open
    // with nothing left to close it.
    expect(VIEWS).toContain("const expandable = isExpandable(task);");
    expect(VIEWS).toContain("const open = expandable && requested;");
    // Both ways in run the SAME function, so the mouse and the keyboard cannot
    // drift apart — and the toggle is guarded inside it.
    expect(ROW).toContain("onClick={activate}");
    expect(ROW).toMatch(/onKeyDown=\{\(e\) => \{[\s\S]*?activate\(\);/);
    expect(ACTIVATE).toContain("if (expandable) onToggle();");
    // A row with no disclosure does not claim one. `false` would say "collapsed,
    // press to expand", which is a promise it cannot keep.
    expect(ROW).toContain("aria-expanded={expandable ? open : undefined}");
  });

  it("leaves every other thing the row does alone", () => {
    // Still a focusable control WHEN THERE IS SOMETHING TO PRESS — and on a leaf
    // row there now is, which is what that focus is for (see the leaf-click block
    // below). The `pressable` guard is the never-run row's business and is pinned
    // in "a row with nothing to open" further down.
    expect(ROW).toContain('role={pressable ? "button" : undefined}');
    expect(ROW).toContain("tabIndex={pressable ? 0 : undefined}");
    // Still the same one gesture that OPENS a multi-message conversation, and it is
    // still a button of its own rather than the row's click; pressing it still goes
    // through the shared performer, so it still clears the thread on the way out.
    // (It is behind SHOW_ROW_ACTIONS as of 2026-08-17, which is about whether it is
    // DRAWN — see "the hidden row actions" — not about what it is.)
    expect(ROW).toContain("{chat && (");
    expect(ROW).toContain("openChat(chat)");
    expect(VIEWS).toContain("const chat = openThreadIntent(task, unread);");
    expect(VIEWS).toMatch(/const openChat = \(intent: OpenThreadIntent\) => \{[\s\S]*?performOpen\(/);
    // Its own presence is decided by openThreadIntent — a session, not a message
    // count — so shortening the thread cannot take the button away.
    expect(VIEWS).not.toMatch(/\{chat && expandable/);
    // And the row's own click still never opens a THREAD on an EXPANDABLE row: a
    // multi-message row toggles and nothing else, because expanding shows the
    // reader nothing and a press that cleared its unread would clear news nobody
    // has seen. The row div carries no openChat of its own — the only way in is
    // `activate`, where the first arm claims every expandable row before the
    // openChat arm can be reached (pinned in "a row with no message at all").
    expect(ROW).not.toMatch(/onClick=\{\(\) => \{\s*openChat/);
    expect(ACTIVATE.indexOf("if (expandable) onToggle();")).toBeLessThan(
      ACTIVATE.indexOf("openChat"),
    );
  });
});

// ---- what a click on a LEAF row does -------------------------------------------
// Dropping the chevron left a one-message row's press doing nothing at all, which
// Akshil noticed and disliked (2026-08-17). With nothing to expand, "open it" is
// the only thing the press can mean — and what it opens is that one message,
// through the message row's own path.

describe("a one-message row's click", () => {
  it("names its single message, and names nothing on a task that never ran", () => {
    const one = task({ message_count: 1, messages: [msg({ message_id: "MSG-001" })] });
    expect(soleMessage(one)!.message_id).toBe("MSG-001");
    // Nothing to open: no transcript, no message, so the row must stay inert
    // rather than navigate somewhere half-built.
    expect(soleMessage(task({ message_count: 0, messages: [] }))).toBe(null);
    // ...and an ACCORDION is never this: its press is the disclosure. Even one
    // holding a window of a single message, which a busy thread can arrive as.
    expect(soleMessage(task({ message_count: 40, messages: [msg()] }))).toBe(null);
    // Asked of what the row HOLDS, so it is the same list the row's mark and its
    // count are arithmetic over.
    const held = [msg({ message_id: "MSG-009" })];
    expect(soleMessage(one, held)!.message_id).toBe("MSG-009");
  });

  it("goes through the MESSAGE row's own path, not a second one", () => {
    // openMessage is the message row's click handler: it marks that one message
    // read and navigates to messageHref. The leaf row calls exactly it, so there
    // is no second place a message's address is built.
    expect(VIEWS).toMatch(
      /const activate = \(\) => \{\s*if \(expandable\) onToggle\(\);\s*else if \(edit\) onEditEntry\?\.\(edit\);\s*else if \(sole\) openMessage\(sole\);\s*else if \(chat\) openChat\(chat\);\s*\};/,
    );
    expect(VIEWS).toContain("const sole = soleMessage(task, held);");
    const at = VIEWS.indexOf("const openMessage = (m: TaskMessage)");
    const fn = VIEWS.slice(at, VIEWS.indexOf("\n  };", at));
    expect(fn).toContain("onRead(task.key, m);");
    expect(fn).toContain("messageHref(task, m)");
    // ONE definition of that path, and the leaf press does not add another: no
    // second navigateUrl and no second href built anywhere in the row.
    expect((VIEWS.match(/messageHref\(task, m\)/g) ?? []).length).toBe(1);
    // Which means the press CLEARS that message's unread — the same clear the
    // message row's own click makes, on the same message, because it is the same
    // call. It does not mark the whole task: openMessage never does.
    expect(fn).not.toContain("markWholeTaskRead");
    expect(fn).not.toContain("onReadAll");
  });

  it("hands off rather than hopping when its own message has nowhere to go", () => {
    // Two guards, and both are needed. `sole` is null on a task with no message,
    // so this arm is not the one that answers for it (the thread arm below is)...
    expect(VIEWS).toContain("else if (sole) openMessage(sole);");
    // ...and openMessage navigates only if there is somewhere to go, which covers
    // the one message on a task with no session yet (messageHref answers null).
    const at = VIEWS.indexOf("const openMessage = (m: TaskMessage)");
    const fn = VIEWS.slice(at, VIEWS.indexOf("\n  };", at));
    expect(fn).toContain("if (to) navigateUrl(to);");
    expect(messageHref(task({ session_id: "" }), msg())).toBe(null);
  });
});

// ---- what a click on a row with NO message does ---------------------------------
// The leaf arm above left one shape of row still doing nothing: zero messages.
// `expandable` is message_count > 1 and `sole` is exactly one, so a task with none
// fell through both and the press was inert on a row that looked pressable (Akshil,
// 2026-08-17: "the (untitled) aren't clickable").
//
// It is the minority path — the sibling change that recovers a real first message
// took these from 43 rows to 10 on live data — and the 10 are worth opening: four
// hand-written fixture transcripts, and six sessions whose only user records were
// slash-command envelopes (`/clear`, `/making-a-release`, `/mcp`, `/capture-idea`),
// which still hold assistant turns to read. What has no session AT ALL is a
// `pending:<entry>` that has never run, and that one stays inert — and now says so.

describe("a row with no message at all", () => {
  /** Zero messages, and a session to open. */
  const ran = task({ message_count: 0, messages: [] }, 0);
  /** Zero messages and never run: §5 mints the session id on the first run. */
  const never = task({ key: "pending:e1", session_id: "", message_count: 0, messages: [] }, 0);

  it("opens the thread, through the one intent the Open chat button asked", () => {
    // The third arm, in the shared handler — so the keyboard reaches it too (the
    // row div wires onClick and onKeyDown to this same `activate`, pinned above).
    expect(ACTIVATE).toContain("else if (chat) openChat(chat);");
    // Not a second url and not a second performer: `chat` is the row's existing
    // openThreadIntent value and openChat is the row's existing performOpen call.
    expect(VIEWS).toContain("const chat = openThreadIntent(task, unread);");
    expect(VIEWS).toMatch(
      /const openChat = \(intent: OpenThreadIntent\) => \{[\s\S]*?performOpen\(/,
    );
    // taskHref stays the only place a thread's address is built — this file still
    // never calls it, so the arm cannot have grown its own href.
    expect(VIEWS).not.toContain("taskHref(");
    // And where it lands is the thread, top of the chat, with no per-turn anchor.
    expect(openThreadIntent(ran)!.href).toBe(taskHref(ran)!);
  });

  it("marks nothing on the way — there is no message to mark", () => {
    // Not a special case in the row: with the whole thread in hand (all none of
    // it) taskUnread's count IS the dots, counted, so it is 0...
    expect(taskUnread(ran, new Set())).toBe(0);
    // ...and openThreadIntent's mark is `unread > 0`, so the intent says no.
    expect(openThreadIntent(ran, taskUnread(ran, new Set()))!.markRead).toBe(false);
    // performOpen puts its whole mark — local clear and server write both — inside
    // that one `if`, and navigates OUTSIDE it, so a false flag writes nothing at
    // all and still opens.
    const at = VIEWS.indexOf("function performOpen(");
    const fn = VIEWS.slice(at, VIEWS.indexOf("\n}", at));
    const guard = fn.slice(fn.indexOf("if (intent.markRead) {"), fn.indexOf("\n  }"));
    expect(guard).toContain("marks.clearAll(task, held);");
    expect(guard).toContain("markWholeTaskRead(task.key)");
    expect(guard).not.toContain("navigateUrl");
    expect(fn).toContain("navigateUrl(intent.href);");
  });

  it("stays inert with no session — and stops advertising a press", () => {
    // Nothing to open: §5 has not minted the id yet, so all four arms decline.
    expect(upcomingEditEntry(never)).toBe(null);
    expect(soleMessage(never)).toBe(null);
    expect(openThreadIntent(never)).toBe(null);
    expect(isExpandable(never)).toBe(false);
    // Which is exactly what `pressable` is: the arms of `activate` that can make a
    // row a control, so the affordance cannot drift from the behaviour. (The edit arm
    // is a narrowing of the leaf arm — it requires soleMessage — so it adds nothing
    // here; that is pinned where the upcoming arm is.)
    expect(VIEWS).toContain(
      "const pressable = expandable || sole !== null || chat !== null;",
    );
    // The row then claims no role and takes no tab stop...
    expect(ROW).toContain('role={pressable ? "button" : undefined}');
    expect(ROW).toContain("tabIndex={pressable ? 0 : undefined}");
    // ...and drops the pointer cursor and the hover tint with a class, which must
    // outrank `.tasks-row` and `.tasks-row:hover` without !important.
    expect(ROW).toContain('(pressable ? "" : " is-inert")');
    expect(block(TASKS_CSS, ".tasks-row.is-inert")).toContain("cursor: default");
    expect(block(TASKS_CSS, ".tasks-row.is-inert:hover")).toContain(
      "background: transparent",
    );
    expect(block(TASKS_CSS, ".tasks-row.is-inert")).not.toContain("!important");
    expect(block(TASKS_CSS, ".tasks-row.is-inert:hover")).not.toContain("!important");
  });

  it("leaves the other two shapes of row exactly as they were", () => {
    // EXACTLY ONE message is still the message anchor, not the thread: the leaf
    // arm comes first, so `chat` — which a leaf row also has — never answers for
    // it, and where it lands still carries the turn's own `msg=`.
    const one = task({ message_count: 1, messages: [msg({ anchor: "uuid-7" })] });
    expect(soleMessage(one)!.anchor).toBe("uuid-7");
    expect(ACTIVATE.indexOf("openMessage(sole)")).toBeLessThan(
      ACTIVATE.indexOf("openChat(chat)"),
    );
    expect(messageHref(one, soleMessage(one)!)).toBe(
      `${taskHref(one)!}&${MESSAGE_ANCHOR_PARAM}=uuid-7`,
    );
    expect(pressableFor(one)).toBe(true);

    // TWO OR MORE still toggles and opens nothing: the accordion arm sits above
    // both opening arms, so a row with a session (every expandable row has one)
    // cannot reach openChat.
    const many = task({ message_count: 5 }, 5);
    expect(isExpandable(many)).toBe(true);
    expect(soleMessage(many)).toBe(null);
    expect(ACTIVATE.indexOf("onToggle()")).toBeLessThan(
      ACTIVATE.indexOf("openChat(chat)"),
    );
    expect(pressableFor(many)).toBe(true);

    // And the zero-message-with-a-session row is pressable too, which is the
    // whole change; only the never-run row is not.
    expect(pressableFor(ran)).toBe(true);
    expect(pressableFor(never)).toBe(false);
  });
});

// ---- what a click on an UPCOMING row does ---------------------------------------
// A LEAF row's press opened its one message — a transcript turn — and for an
// Upcoming task that is the wrong content: its whole point is the instruction that
// has not run yet, which lives in the form (Akshil, 2026-08-17: "when i click on
// upcoming tasks i think they should open up the edit modal", then narrowed to
// "this should be only for 1 message tasks").
//
// So Edit is a NARROWING of the leaf arm, not a lane-wide override. Which is why
// the accordion stays first and the chevron never has to become a control: a
// repeating task with past runs still toggles on its own press, whatever its lane,
// so one click can never both expand a row and open a form.

describe("an upcoming row's click", () => {
  /** Repeating, already run three times, and its next run named: NOT this arm. */
  const soon = task(
    {
      status: "upcoming",
      message_count: 3,
      next_run: Math.floor(Date.parse("2026-08-18T09:00:00") / 1000),
      next_run_entry: "e-next",
    },
    3,
  );
  /** A one-off that has never run: the `pending:<entry>` shape, holding its own
   *  pending message and nothing else. */
  const oneOff = task({
    key: "pending:e9",
    session_id: "",
    status: "upcoming",
    message_count: 1,
    messages: [msg({ message_id: "MSG-001", state: "pending", entry_id: "e9", ran_at: 0 })],
  });

  it("claims a ONE-message upcoming row, and never a multi-message one", () => {
    // The lane comes from taskColumn — the Board's own filing function — so the two
    // views cannot disagree about what "Upcoming" means.
    expect(taskColumn(oneOff)).toBe("upcoming");
    expect(upcomingEditEntry(oneOff)).toBe("e9");
    // THREE PAST MESSAGES AND UPCOMING is the accordion, not the form: this inherits
    // soleMessage's isExpandable guard, so the narrowing is not a second predicate.
    expect(taskColumn(soon)).toBe("upcoming");
    expect(soleMessage(soon)).toBe(null);
    expect(upcomingEditEntry(soon)).toBe(null);
    // Every other lane declines too, so a row that has run is never the form.
    expect(upcomingEditEntry(task({ status: "done" }))).toBe(null);
    expect(upcomingEditEntry(task({ status: "failed" }))).toBe(null);
    expect(upcomingEditEntry(task({ status: "in_progress" }))).toBe(null);
    // The COUNT is asked of what the row HOLDS, as soleMessage's is — so a row the
    // listing calls one message but whose fetched thread holds two is the accordion,
    // not the form.
    expect(
      upcomingEditEntry(oneOff, [msg({ message_id: "MSG-001" }), msg({ message_id: "MSG-002" })]),
    ).toBe(null);
  });

  it("names the entry run-now and the drag would name — never a second one", () => {
    // runNowTarget's answer, so Edit and Run now act on the same run: a one-off's is
    // the pending message it holds, and a row that names a run it does not hold uses
    // the server's `next_run_entry`.
    expect(runNowIntent(oneOff)!.entryId).toBe(upcomingEditEntry(oneOff)!);
    const named = task({
      status: "upcoming",
      message_count: 1,
      messages: [msg({ state: "sent", entry_id: "e-old" })],
      next_run: Math.floor(Date.parse("2026-08-18T09:00:00") / 1000),
      next_run_entry: "e-next",
    });
    expect(upcomingEditEntry(named)).toBe("e-next");
    expect(runNowIntent(named)!.entryId).toBe("e-next");
  });

  it("sits between the accordion and the message, through the one callback", () => {
    // Arm 2 of five: after the toggle, before openMessage — and in the shared
    // handler, so the keyboard reaches it identically (the row wires onClick and
    // onKeyDown to this same `activate`).
    expect(ACTIVATE).toContain("else if (edit) onEditEntry?.(edit);");
    expect(ACTIVATE.indexOf("onToggle()")).toBeLessThan(
      ACTIVATE.indexOf("onEditEntry?.(edit)"),
    );
    expect(ACTIVATE.indexOf("onEditEntry?.(edit)")).toBeLessThan(
      ACTIVATE.indexOf("openMessage(sole)"),
    );
    // `onEditEntry` is the callback the thread's own Edit button and the calendar
    // popover already spend — Scheduled.tsx resolves the entry, and an occurrence to
    // its template — so the form has one way in and this arm builds nothing.
    expect(VIEWS).toContain(
      "const edit = onEditEntry ? upcomingEditEntry(task, held) : null;",
    );
    expect(ACTIVATE).not.toContain("setEditing");
    expect(ACTIVATE).not.toContain("?edit=");
    // And nothing is marked read on the way: the message it opens the form for has
    // not gone out, so `activate`'s edit arm touches no mark at all.
    expect(ACTIVATE).not.toContain("onRead");
    expect(ACTIVATE).not.toContain("markRead");
  });

  it("leaves the chevron a glyph, because the row still toggles", () => {
    // The accordion is arm ONE, so no lane takes the toggle off a multi-message row
    // and the chevron never needs a press of its own — nothing to double-fire.
    expect(ACTIVATE).toMatch(/^\s*const activate = \(\) => \{\s*if \(expandable\) onToggle\(\);/);
    const caret = ROW.slice(ROW.indexOf('"tasks-caret"'));
    const span = caret.slice(0, caret.indexOf("</span>"));
    expect(span).toContain("aria-hidden");
    expect(span).not.toContain("role=");
    expect(span).not.toContain("tabIndex");
    expect(span).not.toContain("onClick");
    expect(span).not.toContain("onKeyDown");
    // aria-expanded stays the ROW's own, and there is exactly one of it.
    expect(ROW).toContain("aria-expanded={expandable ? open : undefined}");
    expect((ROW.match(/aria-expanded=/g) ?? []).length).toBe(1);
  });

  it("falls through rather than opening a blank form when the entry is unknown", () => {
    // The one message is not pending (or is a chat, which carries no entry_id) and
    // the server named no next run — then there is nothing to edit and the row means
    // what it meant before: open that message.
    const vague = task({
      status: "upcoming",
      message_count: 1,
      messages: [msg({ kind: "chat", state: "pending", entry_id: "" })],
    });
    expect(upcomingEditEntry(vague)).toBe(null);
    expect(soleMessage(vague)).not.toBe(null);
    expect(pressableFor(vague)).toBe(true);
    // And with nothing to expand or open either, it is honestly inert.
    expect(
      pressableFor(
        task({ status: "upcoming", session_id: "", message_count: 0, messages: [] }, 0),
      ),
    ).toBe(false);
  });
});

/** `pressable` as the row computes it — the arms of `activate` that can make a row
 *  a control, evaluated here so the cases can be asserted as VALUES rather than only
 *  as source. The edit arm is absent for the reason the row's own comment gives: it
 *  requires soleMessage, so it is a narrowing of the leaf arm and can never make a
 *  row pressable that `sole` did not already. */
function pressableFor(t: Task): boolean {
  const sole = soleMessage(t);
  const chat = openThreadIntent(t, taskUnread(t, new Set()));
  return isExpandable(t) || sole !== null || chat !== null;
}

// ---- what a click on a MESSAGE row does -----------------------------------------
// The same principle one level down (Akshil, 2026-08-17: "for multi-message tasks
// when i click on the message, that should open the edit modal"): a message that has
// not gone out is an INSTRUCTION and belongs in the form; one that has run is a
// TRANSCRIPT TURN and belongs in the transcript. A form over a spent entry would
// present a Save that means nothing, which is why the split is not "every message
// row goes to the form".

describe("a message row's click", () => {
  it("edits a message that has not gone out, on its OWN entry", () => {
    expect(messageEditEntry(msg({ state: "pending", entry_id: "e-mine" }))).toBe("e-mine");
    // Its own, never the task's next run: a repeating task has several pending
    // occurrences and the row pressed is the one the reader means. Resolving an
    // occurrence UP to its template is Scheduled.tsx's job, as it already is for the
    // pencil.
    expect(
      messageEditEntry(msg({ state: "pending", entry_id: "occ-3", template_id: "tpl-1" })),
    ).toBe("occ-3");
  });

  it("sends every message that HAS run to its turn in the transcript", () => {
    // `sending` is mid-flight and already beyond changing; the four settled states
    // have had their whole life. All five keep today's behaviour.
    for (const state of ["sending", "sent", "missed", "error", "cancelled"] as const) {
      expect(messageEditEntry(msg({ state, entry_id: "e1" }))).toBe(null);
    }
    // Including the ones that went wrong — missed, failed and cancelled have run, so
    // the transcript is still where they are read.
    expect(messageEditEntry(msg({ state: "sent", turn: "unknown", entry_id: "e1" }))).toBe(
      null,
    );
    // It is the message's own `state`, not turnPhase: turnPhase reads `turn`, which
    // answers how the SESSION replied and is a question only a sent message has.
    const fn = LIB.slice(
      LIB.indexOf("export function messageEditEntry("),
      LIB.indexOf("\n}", LIB.indexOf("export function messageEditEntry(")),
    );
    expect(fn).toContain('m.state !== "pending"');
    expect(fn).not.toContain("turnPhase");
    // The same predicate Cancel asks, deliberately: a message the server would
    // refuse to cancel is one it would refuse to edit.
    expect(cancelIntent(msg({ state: "pending", entry_id: "e-mine" }))!.id).toBe("e-mine");
    expect(canCancel(msg({ state: "sent", entry_id: "e1" }))).toBe(false);
  });

  it("falls through to the transcript when a pending message names no entry", () => {
    // A CHAT message was delivered the moment it was typed, so the schedule has no
    // record of it to open a form on.
    expect(messageEditEntry(msg({ kind: "chat", state: "pending", entry_id: "" }))).toBe(
      null,
    );
  });

  it("runs both meanings through ONE handler, and marks only the transcript one", () => {
    // The message row's click and its Enter/Space are the same function, exactly as
    // the task row's are.
    expect(THREAD).toContain("onClick={() => pressMessage(m)}");
    expect(THREAD).toMatch(/onKeyDown=\{\(e\) => \{[\s\S]*?pressMessage\(m\);/);
    const at = VIEWS.indexOf("const pressMessage = (m: TaskMessage)");
    const fn = VIEWS.slice(at, VIEWS.indexOf("\n  };", at));
    expect(fn).toContain("const entry = onEditEntry ? messageEditEntry(m) : null;");
    expect(fn).toContain("if (entry) onEditEntry?.(entry);");
    expect(fn).toContain("else openMessage(m);");
    // The edit arm marks nothing — a message that has not happened is not unread in
    // the first place...
    expect(fn).not.toContain("onRead(task.key, m)");
    expect(isUnread("sess-1", msg({ state: "pending", ran_at: 0 }), new Set())).toBe(false);
    // ...and the transcript arm keeps the per-message mark it always had.
    const open = VIEWS.slice(
      VIEWS.indexOf("const openMessage = (m: TaskMessage)"),
      VIEWS.indexOf("\n  };", VIEWS.indexOf("const openMessage = (m: TaskMessage)")),
    );
    expect(open).toContain("onRead(task.key, m);");
    // And the quiet pencil reads the very same answer, so the two cannot disagree
    // about which rows are editable.
    expect(THREAD).toContain("const fix = onEditEntry ? messageEditEntry(m) : null;");
    expect(THREAD).toContain("{fix && (");
    expect(THREAD).toContain("onEditEntry?.(fix);");
    expect(THREAD).not.toContain('m.state === "pending" && m.entry_id');
  });
});

// ---- where Archive is drawn ----------------------------------------------------
// Two claims the pure half cannot hold: that the action EXISTS on both views,
// and that it is silent until the row or card is pointed at. The second is the
// whole reason it was allowed onto a row at all, so it is read out of the source
// rather than left to a screenshot.

describe("the archive action", () => {
  it("sits in the List row's hover-revealed group, conditional on the intent", () => {
    const from = VIEWS.indexOf('className={"tasks-row"');
    const row = VIEWS.slice(from, VIEWS.indexOf("{open && (", from));
    // Only when there is something to file — a session-less row grows nothing.
    expect(row).toContain("{file && (");
    // The same class Run now / Edit / Cancel wear, which is what makes it quiet.
    expect(row).toMatch(/"tasks-act (?:.|\n)*?tasks-act--unarchive/);
    expect(row).toContain("ICON_ARCHIVE");
    expect(row).toContain("ICON_UNARCHIVE");
    // Both directions, and the label comes from tasks-lib rather than the row.
    expect(row).toContain("file.status");
    expect(row).toContain("aria-label={file.label}");
  });

  it("is absent at rest and present on hover or focus", () => {
    // The group's resting state is invisible...
    const head = TASKS_CSS.indexOf(".tasks-act,\n.prefs-section .tasks-act {");
    expect(head).toBeGreaterThan(-1);
    expect(TASKS_CSS.slice(head, TASKS_CSS.indexOf("}", head))).toContain("opacity: 0;");
    // ...and both a pointer and a keyboard bring it back, on the row...
    expect(TASKS_CSS).toContain(".tasks-row:hover .tasks-act");
    expect(TASKS_CSS).toContain(".tasks-row:focus-within .tasks-act");
    // ...and on a board card, which is not a row and needs its own pair.
    expect(TASKS_CSS).toContain(".tasks-card-wrap:hover .tasks-card-act");
    expect(TASKS_CSS).toContain(".tasks-card-wrap:focus-within .tasks-card-act");
    // Reachable with a visible ring either way.
    expect(TASKS_CSS).toContain(".tasks-act:focus-visible");
    // House rule: never the property that animates layout and skin together.
    // As a DECLARATION — the file's own header names it in prose to forbid it.
    expect(TASKS_CSS).not.toMatch(/^\s*transition: all/m);
  });

  it("gives the board card the same action, so the drag is not the only way", () => {
    const card = VIEWS.slice(VIEWS.indexOf("function TaskCard("));
    expect(card).toContain("archiveIntent(task)");
    expect(card).toContain("tasks-card-act");
    // A button cannot be nested inside a button, which is what the wrapper is
    // for — and what it is positioned against.
    expect(card).toContain("tasks-card-wrap");
    expect(TASKS_CSS).toMatch(/\.tasks-card-wrap\s*\{[^}]*position: relative/);
  });

  it("shares one strip with Run now instead of stacking on it", () => {
    // A failed task whose message is spent offers Run now AND Archive, and two
    // siblings each pinned to the same `right` would sit on top of each other.
    const card = VIEWS.slice(VIEWS.indexOf("function TaskCard("));
    // Behind SHOW_ROW_ACTIONS since 2026-08-17 — see "the hidden row actions" —
    // but the strip and its one-pin arrangement are what that flag brings back.
    expect(card).toContain("(run || file) && (");
    expect(card).toContain('className="tasks-card-acts"');
    expect(TASKS_CSS).toMatch(/\.tasks-card-acts\s*\{[^}]*position: absolute/);
    expect(TASKS_CSS).toMatch(/\.tasks-card-acts\s*\{[^}]*display: flex/);
    // The strip is invisible chrome over a card that IS a button, so the gap
    // between its children must not swallow the press that opens the chat.
    expect(TASKS_CSS).toMatch(/\.tasks-card-acts\s*\{[^}]*pointer-events: none/);
    expect(TASKS_CSS).toMatch(
      /\.tasks-card-act,\n\.prefs-section \.tasks-card-act \{[^}]*pointer-events: auto/,
    );
  });

  it("never paints it red — archiving destroys nothing", () => {
    // Cancel's hue is the destructive one and the two are one flick apart; using
    // it here would assert the very thing archiving exists to deny.
    const at = TASKS_CSS.indexOf(".tasks-act--archive:hover");
    expect(at).toBeGreaterThan(-1);
    expect(TASKS_CSS.slice(at, TASKS_CSS.indexOf("}", at))).not.toContain("--error");
  });
});

// ---- where Run now and Mark read are drawn -------------------------------------
// The Board had no run action at all: it was on the List row and in the calendar
// popover and simply missing from the kanban card (Akshil, 2026-08-17: "I have a
// rerun option in list, I have a rerun option in calendar, but I don't have a
// rerun option in Kanban"). And clearing a task's unread was one click per
// message. Both claims are about WHERE a control is, so both are read out of the
// source rather than left to a screenshot.

const NODE = VIEWS.slice(
  VIEWS.indexOf("function TaskNode("),
  VIEWS.indexOf("export function TaskBoard("),
);
const BOARD = VIEWS.slice(
  VIEWS.indexOf("export function TaskBoard("),
  VIEWS.indexOf("function TaskCard("),
);
const CARD = VIEWS.slice(VIEWS.indexOf("function TaskCard("));
/** The row's own gesture, on both the pointer and the keyboard. */
const ACTIVATE = (() => {
  const at = VIEWS.indexOf("const activate = () => {");
  return VIEWS.slice(at, VIEWS.indexOf("\n  };", at));
})();
/** The List's task row, which ends where the thread it can open begins. */
const ROW = (() => {
  const from = VIEWS.indexOf('className={"tasks-row"');
  return VIEWS.slice(from, VIEWS.indexOf("{open && (", from));
})();
/** The expanded thread — one message row per message — which is where ROW stops. */
const THREAD = VIEWS.slice(
  VIEWS.indexOf("{open && (", VIEWS.indexOf('className={"tasks-row"')),
  VIEWS.indexOf("export function TaskBoard("),
);

describe("the run action on a board card", () => {
  it("offers the intent the List row offers, from the same function", () => {
    // Not a second predicate and not a second entry id: both sides ask
    // taskRunIntent, which asks runNowIntent — the function dropAction asks — so
    // the card's button, the row's button and the drag cannot pick different
    // messages.
    expect(NODE).toContain("taskRunIntent(task)");
    expect(CARD).toContain("taskRunIntent(task)");
    expect(CARD).toContain("onRun(intent)");
    // The word comes from the intent as well, both halves of it.
    expect(CARD).toContain("{run.rerun ? ICON_RERUN : ICON_PLAY}");
    expect(CARD).toContain("run.title");
    expect(CARD).toContain("run.label");
  });

  it("spends the intent through the ONE function both views share", () => {
    // Two copies of the run-now/resend switch is how two views start disagreeing
    // about what "Re-run" does.
    expect(NODE).toContain("performRun(intent)");
    expect(BOARD).toContain("performRun(intent)");
    expect((VIEWS.match(/resendScheduledMessage\(/g) ?? []).length).toBe(1);
  });

  it("lands a refusal in the board's own note line, not inside a lane", () => {
    // A 409 (that conversation has a turn open) is "wait", not "broken", and it
    // is unreadable tucked under one card in a 260px column — so the call lives
    // on the board and the card only asks for it.
    expect(BOARD).toContain("const runNow = async (intent: TaskRunIntent)");
    expect(BOARD).toContain("setNote((e as Error).message)");
    expect(CARD).not.toContain("runScheduledNow");
  });

  it("is hidden while the card is in the air, like Archive", () => {
    expect(CARD).toContain("tasks-card-act");
    expect(TASKS_CSS).toContain(".tasks-card-wrap.is-dragging .tasks-card-act");
  });
});

describe("the mark-read action", () => {
  it("sits in the List row's hover-revealed group, conditional on the intent", () => {
    // Same group as Run now and Archive, so a list at rest grows no chrome.
    expect(ROW).toContain("{seen && (");
    expect(ROW).toContain("tasks-act--seen");
    expect(ROW).toContain("ICON_MARK_READ");
    expect(ROW).toContain("aria-label={seen.label}");
    // Whether it exists at all is the intent's decision, asked with the count the
    // row is drawing rather than the raw server number.
    expect(NODE).toContain("markReadIntent(task, read, held)");
  });

  it("is ONE request for the whole thread, not one per message", () => {
    expect(NODE).toContain("markWholeTaskRead(task.key)");
    // The per-message call is still exactly what a message CLICK makes, and
    // nothing here loops over messages.
    expect(VIEWS).toContain("markTaskMessageRead(taskKey, m.message_id)");
  });

  it("clears the local set too, so the dots go on the click", () => {
    expect(NODE).toContain("onReadAll(task, held)");
    expect(VIEWS).toContain("markAllRead(cur, task, held)");
  });

  it("reconciles the optimism instead of planting it and walking away", () => {
    // The press used to write a mark nothing could ever remove: a refusal left
    // the row looking read with this very button gone, so there was no retry.
    const at = NODE.indexOf("const markSeen = async () => {");
    expect(at).toBeGreaterThan(-1);
    const fn = NODE.slice(at, NODE.indexOf("\n  };", at));
    // The ids the rollback will need, captured BEFORE the await — a poll can
    // replace the thread while the request is in flight.
    expect(fn.indexOf("const wrote = held;")).toBeLessThan(
      fn.indexOf("await markWholeTaskRead"),
    );
    // ...plus whatever the thread holds by the time the answer lands: a Show more
    // that arrived meanwhile carried this very mark onto the rest of the thread
    // (useReadSet.carryAll), and those ids are the press's too.
    expect(fn).toContain("[...wrote, ...heldNow.current]");
    // Refused: the mark comes back off, and the server's sentence is said.
    expect(fn).toContain("onUnreadAll(task.key, rollback())");
    expect(fn).toContain("setNote((e as Error).message)");
    // 200 with something still unread: that wins too, rather than being dropped.
    expect(fn).toContain("if (answer.unread > 0)");
    expect(fn).toContain("onSettleAll(task.key, rollback(), answer)");
    // Still no reload — the row has already said the one thing it knows.
    expect(fn).not.toContain("onReload");
  });

  it("carries a standing mark onto the thread Show more fetches", () => {
    // The fetch's reply is a read of a value the press may already have
    // overridden, and nothing refetches it (`more` is false by then). So the
    // fetch adopts the standing mark — asked of the FRESHEST task, because "is
    // the mark still standing?" is a question about the newest poll, not about
    // the render the button was pressed in.
    const at = VIEWS.indexOf("const showMore = async (task: Task) => {");
    expect(at).toBeGreaterThan(-1);
    const fn = VIEWS.slice(at, VIEWS.indexOf("\n  };", at));
    expect(fn.indexOf("const thread = r.messages ?? [];")).toBeLessThan(
      fn.indexOf("carryAll(fresh, thread)"),
    );
    expect(fn).toContain("latest.current.find((t) => t.key === task.key) ?? task");
    expect(VIEWS).toContain("carryMarkToHeld(cur, task, held)");
    // ...and the ref it reads that from is kept up to date by the poll.
    expect(VIEWS).toContain("latest.current = tasks;");
  });

  it("never wears the unread dot's own hue", () => {
    // --activity is what the thread's dots are painted in, and what Run now takes
    // on hover — the button directly beside this one.
    const at = TASKS_CSS.indexOf(".tasks-act--seen:hover");
    expect(at).toBeGreaterThan(-1);
    const rule = TASKS_CSS.slice(at, TASKS_CSS.indexOf("}", at));
    expect(rule).not.toContain("--activity");
    expect(rule).not.toContain("--error");
  });
});

// ---- the hidden row actions -----------------------------------------------------
// Akshil, 2026-08-17: "hide them, keep the functionality but hide them". Every
// intent, handler and call above is still exactly as tested; what changed is that
// nothing DRAWS the strip. Both claims are read out of the source, because "hidden"
// is only half the requirement — the other half is that nothing was deleted.

describe("the hidden row actions", () => {
  it("is one named flag, off, and it gates the RENDER on both views", () => {
    expect(VIEWS).toMatch(/const SHOW_ROW_ACTIONS: boolean = false;/);
    // The List row's whole strip behind it, and the Board card's too — one flag, so
    // the two views cannot diverge when it flips.
    expect(ROW).toContain("{SHOW_ROW_ACTIONS && (");
    expect(CARD).toContain("{SHOW_ROW_ACTIONS && (run || file) && (");
    // THREE guards and no more: the task row's strip, the board card's strip, and
    // the message row's Edit/Cancel pair — every hover-revealed action in the List.
    // (The name also appears in prose, which is not a gate.)
    expect((VIEWS.match(/SHOW_ROW_ACTIONS &&/g) ?? []).length).toBe(3);
    const msgFrom = VIEWS.indexOf('className={"tasks-msg"');
    const msgRow = VIEWS.slice(msgFrom, VIEWS.indexOf("{why && <p", msgFrom));
    expect(msgRow).toContain("{SHOW_ROW_ACTIONS && (");
    // Who asked, and when, beside the flag — restoring it is one word and the
    // reader has to be able to tell whether the reason still holds.
    const at = VIEWS.indexOf("const SHOW_ROW_ACTIONS");
    const why = VIEWS.slice(VIEWS.lastIndexOf("/**", at), at);
    expect(why).toContain("Akshil");
    expect(why).toContain("2026-08-17");
  });

  it("does not leave an invisible focusable button behind", () => {
    // The trap this avoids: `.tasks-act` rests at `opacity: 0`, so hiding the strip
    // with CSS would leave buttons in the tab order that a keyboard could focus and
    // press blind. They are NOT RENDERED instead — which is why the flag wraps the
    // JSX and not a class name.
    expect(VIEWS).not.toMatch(/SHOW_ROW_ACTIONS \?[^\n]*"is-hidden"/);
    expect(TASKS_CSS).not.toMatch(/\.tasks-act\s*\{[^}]*display: none/);
    expect(TASKS_CSS).not.toMatch(/\.tasks-card-acts\s*\{[^}]*display: none/);
    // The reveal rules are untouched — they are what the actions come back to, and
    // the stylesheet says so rather than being tidied away.
    expect(TASKS_CSS).toContain(".tasks-msg:hover .tasks-act");
    expect(TASKS_CSS).toContain(".tasks-row:focus-within .tasks-act");
    // Edit and Cancel are still WRITTEN, just not rendered: the user's second pass
    // covered them too ("hide the hover actions for now, that's what I said", said
    // of the pencil on a message row), so they are behind the same flag.
    const from = VIEWS.indexOf('className={"tasks-msg"');
    const msgRow = VIEWS.slice(from, VIEWS.indexOf("{why && <p", from));
    expect(msgRow).toContain('title="Edit"');
    expect(msgRow).toContain("tasks-act--cancel");
    expect(msgRow.indexOf("{SHOW_ROW_ACTIONS && (")).toBeLessThan(
      msgRow.indexOf('title="Edit"'),
    );
    // ...and the row's own click is NOT behind the flag: a message row still opens
    // its turn — or, since 2026-08-17, the form for a message that has not gone out —
    // hidden actions or not. Which is also what keeps Edit REACHABLE with the pencil
    // hidden: the row press is the way in now.
    expect(msgRow).toContain("onClick={() => pressMessage(m)}");
  });

  it("deletes nothing — every intent, handler and call is still there", () => {
    // The functionality is the half being KEPT. If any of this goes, flipping the
    // flag brings back a strip of buttons that do nothing.
    for (const kept of [
      "markReadIntent(task, read, held)",
      "taskRunIntent(task)",
      "archiveIntent(task)",
      "openThreadIntent(task, unread)",
      "const markSeen = async () => {",
      "const triage = async (status: ArchiveStatus)",
      "const openChat = (intent: OpenThreadIntent)",
    ]) {
      expect(VIEWS).toContain(kept);
    }
    expect(CARD).toContain("void runNow(run)");
    expect(CARD).toContain("void triage(file.status)");
  });

  it("keeps the strip's geometry, which is what it comes back to", () => {
    // Two corrections went into placing this strip on the card's head rather than
    // above it (the `top` is derived from the card's padding and the head's line),
    // and a stylesheet that had forgotten them would bring the strip back in the
    // wrong place.
    expect(TASKS_CSS).toMatch(/\.tasks-card-acts\s*\{[^}]*position: absolute/);
    expect(TASKS_CSS).toMatch(/\.tasks-card-acts\s*\{[^}]*top: 7px/);
    expect(TASKS_CSS).toMatch(/\.tasks-card-acts\s*\{[^}]*pointer-events: none/);
    expect(TASKS_CSS).toMatch(/--tasks-card-head-h: 16px/);
    // And it says so, so the next reader does not tidy away a rule with no live
    // markup behind it.
    expect(TASKS_CSS).toContain("SHOW_ROW_ACTIONS");
  });
});

// ---- two marks after one title -------------------------------------------------
// The unread dot went grey, and that turned the live ping into a collision: two
// filled discs of nearly the same size in the same slot, one grey and one blue
// ("why is there still a blue dot as well", Akshil, 2026-08-17). Hue and an
// animation cannot separate two marks in a still glance, and a screenshot is how
// this page gets read back.

describe("the live mark and the unread mark", () => {
  it("separates them by COLOUR, the live mark's form untouched", () => {
    // A hollow ring for the live mark was proposed and refused ("we don't want
    // that", Akshil, 2026-08-17): the unread mark going grey is the distinction, and
    // it is enough — grey after the title means unread, blue means live, and only
    // the blue one moves. So the ping is still the 8px filled dot it always was.
    const ping = block(SCHEDULE_CSS, ".schedule-tv-pulse");
    expect(ping).toContain("width: 8px");
    expect(ping).toContain("flex: 0 0 8px");
    const marks = SCHEDULE_CSS.match(
      /\.schedule-tv-pulse::before,\n\.schedule-tv-pulse::after \{([^}]*)\}/,
    )?.[1];
    expect(marks).toContain("background: var(--activity)");
    // Filled, not hollow — `border-radius` is the only border word allowed in here.
    expect(marks).not.toContain("border:");
    expect(marks).not.toContain("border-width");
    // ...and the unread dot is the grey one, at its own size.
    const dot = block(TASKS_CSS, ".tasks-dot");
    expect(dot).toContain("background: var(--fg-muted)");
    expect(dot).toContain("width: 7px");
    // Two different tokens, so the difference cannot quietly collapse.
    expect(dot).not.toContain("--activity");
    expect(marks).not.toContain("--fg-muted");
  });

  it("keeps the blue where blue means happening, and keeps it moving", () => {
    // In Progress yellow cannot say "right now" — a queued turn that has not started
    // is In Progress too — so the ping is not removable.
    expect(VIEWS).toContain("export function LivePulse()");
    expect(VIEWS).toMatch(/LivePulse\(\)[\s\S]*?className="schedule-tv-pulse"/);
    expect(SCHEDULE_CSS).toContain("@keyframes schedule-tv-pulse");
    expect(SCHEDULE_CSS).toMatch(
      /@media \(prefers-reduced-motion: no-preference\) \{\s*\.schedule-tv-pulse::before \{[^}]*animation: schedule-tv-pulse/,
    );
  });

  it("puts both on one row without either speaking for the other", () => {
    // A live task with unread messages draws the pair: the grey disc immediately
    // after the title, then the ring. Distinct names, and neither is a bare glyph.
    const dotAt = ROW.indexOf("<UnreadDot");
    const liveAt = ROW.indexOf("<LivePulse");
    expect(dotAt).toBeGreaterThan(-1);
    expect(liveAt).toBeGreaterThan(dotAt);
    expect(liveAt).toBeLessThan(ROW.indexOf('className="tasks-grow"'));
    expect(VIEWS).toMatch(/LivePulse[\s\S]{0,200}aria-label="Running"/);
    expect(VIEWS).toMatch(/function UnreadDot\([\s\S]*?aria-label=\{label\}/);
    expect(taskUnreadLabel(3)).toBe("3 unread messages");
  });
});

// ---- the folder chip, only when it says something -------------------------------

describe("the folder chip on a row and a card", () => {
  it("is drawn only when the shown rows span more than one project", () => {
    // Both views ask the ONE helper, of the set they are drawing, and neither
    // re-derives it per row: a chip that every visible row repeats is noise on the
    // busiest end of the row.
    expect(VIEWS).toContain("const showProject = useMemo(() => spansProjects(tasks), [tasks]);");
    expect((VIEWS.match(/spansProjects\(tasks\)/g) ?? []).length).toBe(2);
    expect(ROW).toContain("{showProject && (");
    expect(CARD).toContain("{showProject && (");
    // The row takes it as a PROP rather than asking per row — the question is about
    // the list, and a row cannot see the list.
    expect(NODE).toContain("showProject: boolean;");
    expect(CARD).toContain("showProject: boolean;");
  });

  it("takes the whole chip away, not the name inside it", () => {
    // On the card the chip is the only thing in the foot, so the foot goes with it
    // rather than leaving a line of padding.
    expect(CARD).toMatch(
      /\{showProject && \(\s*<span className="schedule-tv-card-foot">/,
    );
    // The path is still on the row itself, so nothing is unreachable: the row's
    // `title` is the task's own, and the chip's tooltip was never the only copy.
    expect(ROW).toContain("title={task.title}");
  });
});

// ---- the "ran 07:12 today" label, gone -----------------------------------------

describe("the message row's second time", () => {
  it("draws ONE time, relative, with the absolute pair in its tooltip", () => {
    const from = VIEWS.indexOf('className={"tasks-msg"');
    const msgRow = VIEWS.slice(from, VIEWS.indexOf("{why && <p", from));
    expect(msgRow).toContain("{relativeWhen(m.at)}");
    expect(msgRow).toContain("title={messageWhenTitle(m)}");
    // The label and its rule are gone, and nothing renders the class.
    expect(VIEWS).not.toContain("tasks-msg-ran");
    expect(VIEWS).not.toContain("ranNote");
    expect(TASKS_CSS.replace(/\/\*[\s\S]*?\*\//g, "")).not.toContain("tasks-msg-ran");
    // Exactly one time element on the row.
    expect((msgRow.match(/tasks-msg-time/g) ?? []).length).toBe(1);
  });

  it("still lines a column of them up", () => {
    // Relative or not, "30m ago" over "12h ago" only lines up with tabular figures.
    expect(block(TASKS_CSS, ".tasks-msg-time")).toContain("font-variant-numeric: tabular-nums");
    expect(block(TASKS_CSS, ".tasks-row-time")).toContain("font-variant-numeric: tabular-nums");
  });
});

// ---- the time on a task row -----------------------------------------------------
// Akshil, 2026-08-17: "let's show the time as well for like besides the folder.
// Let's do that for every task". A time was only ever visible on a MESSAGE row
// until now, so a one-message task — which has no thread to expand — showed none.

describe("the time a task row prints", () => {
  const T = (iso: string) => Math.floor(Date.parse(iso) / 1000);

  it("shows an Upcoming task its NEXT run and a finished one its LAST", () => {
    const soon = task({
      status: "upcoming",
      next_run: T("2026-08-21T09:00:00"),
      messages: [msg({ state: "pending", ran_at: 0, at: T("2026-08-21T09:00:00") })],
    });
    expect(taskWhen(soon, NOW)).toMatchObject({ at: T("2026-08-21T09:00:00"), kind: "next" });
    const ran = task({
      status: "done",
      messages: [msg({ at: T("2026-08-16T09:00:00"), ran_at: T("2026-08-16T09:00:00") })],
    });
    expect(taskWhen(ran, NOW)).toMatchObject({ at: T("2026-08-16T09:00:00"), kind: "last" });
  });

  it("takes that rule from LANE_SORTS rather than keeping a second list", () => {
    // The lane sorted by `next-run` is the lane whose reader wants to know when the
    // work happens; every `last-run` lane wants to know when it did. Reading it off
    // that map is what stops a row and the lane it sits in from disagreeing.
    for (const lane of BOARD_COLUMNS.map((c) => c.key)) {
      const both = task({
        status: lane,
        next_run: T("2026-08-21T09:00:00"),
        messages: [
          msg({ message_id: "MSG-002", state: "pending", ran_at: 0,
                at: T("2026-08-21T09:00:00") }),
          msg({ message_id: "MSG-001", at: T("2026-08-16T09:00:00"),
                ran_at: T("2026-08-16T09:00:00") }),
        ],
      });
      const want = LANE_SORTS[taskColumn(both)].key === "next-run" ? "next" : "last";
      expect(taskWhen(both, NOW)!.kind).toBe(want);
    }
  });

  it("falls back to the other time rather than printing nothing", () => {
    // An Upcoming task whose pending message is outside the window still ran once,
    // and that run is the only time it has.
    const upcomingNoNext = task({ status: "upcoming", next_run: 0, messages: [
      msg({ at: T("2026-08-16T09:00:00"), ran_at: T("2026-08-16T09:00:00") }),
    ] });
    expect(taskWhen(upcomingNoNext, NOW)).toMatchObject({ kind: "last" });
    // ...and a Done task with a repeat still coming has a next run to show.
    const doneWithNext = task({ status: "done", next_run: T("2026-08-21T09:00:00"),
      messages: [msg({ state: "pending", ran_at: 0, at: T("2026-08-21T09:00:00") })] });
    expect(taskWhen(doneWithNext, NOW)).toMatchObject({ kind: "next" });
  });

  it("prints NOTHING when the task has neither run — never 1970", () => {
    // A pending row that has never run and has nothing scheduled we can see. Null
    // is the honest answer; epoch zero would format as a confident wrong date.
    const never = task({ status: "upcoming", next_run: 0, message_count: 0, messages: [] });
    expect(nextRunAt(never)).toBe(null);
    expect(lastRunAt(never)).toBe(null);
    expect(taskWhen(never, NOW)).toBe(null);
    expect(ROW).toContain("{when && (");
  });

  it("prints ONE relative unit, and puts the absolute instant in the tooltip", () => {
    const ran = task({ status: "done", messages: [
      msg({ at: T("2026-08-16T09:00:00"), ran_at: T("2026-08-16T09:00:00") }),
    ] });
    const when = taskWhen(ran, NOW)!;
    // The same formatter the message rows below it use, so the row and its thread
    // are one vocabulary rather than two dialects of the same page. NOW is noon and
    // the run was at 09:00.
    expect(when.text).toBe(relativeWhen(T("2026-08-16T09:00:00"), NOW));
    expect(when.text).toBe("3h ago");
    // No clock and no date in the ink — that pair is what made the row's right-hand
    // end too busy. "Which run, exactly when" is the tooltip's job.
    expect(when.text).not.toContain(":");
    expect(when.title).toBe(`Last run ${messageStamp(T("2026-08-16T09:00:00"))}`);
    const soon = taskWhen(task({ status: "upcoming", next_run: T("2026-08-21T09:00:00"),
      messages: [msg({ state: "pending", ran_at: 0, at: T("2026-08-21T09:00:00") })] }), NOW)!;
    expect(soon.title).toContain("Next run");
    // A FUTURE run reads forwards. This is the common Upcoming row.
    expect(soon.text).toBe("in 4d");
  });

  it("says `ago` about an OVERDUE next run, because that is what it is", () => {
    // Scheduling into the past is allowed on this branch and catch-up is unbounded,
    // so an Upcoming task's next run can already have gone by. The direction comes
    // from the instant, never from `kind`, so the row cannot promise a run in the
    // future that the scheduler is late on.
    const late = task({ status: "upcoming", next_run: T("2026-08-16T09:00:00"),
      messages: [msg({ state: "pending", ran_at: 0, at: T("2026-08-16T09:00:00") })] });
    const when = taskWhen(late, NOW)!;
    expect(when.kind).toBe("next");
    expect(when.text).toBe("3h ago");
    expect(when.title).toContain("Next run");
  });

  it("sits after the row's ONE spacer, last in the row, and shrinks nothing", () => {
    const time = ROW.indexOf('className="tasks-row-time"');
    expect(time).toBeGreaterThan(-1);
    // After the spacer — so it belongs to the trailing metadata group rather than
    // hugging the title — and after the folder chip, which is the order the two
    // were swapped into: the last thing before the row's edge is what a reader
    // lands on, and the time is the half that changes.
    expect(time).toBeGreaterThan(ROW.indexOf('className="tasks-grow"'));
    expect(time).toBeGreaterThan(ROW.indexOf("<IdentityChip"));
    // Still exactly one spacer and no auto margin anywhere: free space is split
    // equally among every `auto` margin, so a second would re-centre the group.
    expect((ROW.match(/className="tasks-grow"/g) ?? []).length).toBe(1);
    expect((TASKS_CSS.match(/margin-left: auto/g) ?? []).length).toBe(0);
    // The TITLE is the element that gives way: the time and the folder hold their
    // size, so a long title ellipsises instead of squeezing the time out.
    expect(block(TASKS_CSS, ".tasks-row-time")).toContain("flex: 0 0 auto");
    expect(block(TASKS_CSS, ".tasks-row .schedule-tv-id")).toContain("flex: 0 0 auto");
    const title = block(TASKS_CSS, ".tasks-title");
    expect(title).toContain("min-width: 0");
    expect(title).toContain("text-overflow: ellipsis");
    // A column of times has to line up, exactly as the message rows' do.
    expect(block(TASKS_CSS, ".tasks-row-time")).toContain("font-variant-numeric: tabular-nums");
    expect(block(TASKS_CSS, ".tasks-msg-time")).toContain("font-variant-numeric: tabular-nums");
    // Same weight and colour as a message row's time — one kind of fact, one
    // register.
    for (const decl of ["font-size: 11px", "color: var(--fg-muted)"]) {
      expect(block(TASKS_CSS, ".tasks-row-time")).toContain(decl);
      expect(block(TASKS_CSS, ".tasks-msg-time")).toContain(decl);
    }
  });
});

describe("opening a thread, from either view", () => {
  it("is ONE rule and ONE performer, asked by both gestures", () => {
    // The rule (does this mark?) is tasks-lib's, and neither view re-derives it.
    expect(CARD).toContain("openThreadIntent(task, unread)");
    expect(NODE).toContain("openThreadIntent(task, unread)");
    // The performing half is lifted too, for the same reason performRun was:
    // two copies of "mark local, fire the POST, navigate" is how the two views
    // start disagreeing again. Exactly one definition, and both views spend it.
    expect((VIEWS.match(/function performOpen\(/g) ?? []).length).toBe(1);
    expect(BOARD).toContain(
      "performOpen(task, intent, { clearAll, restoreAll, settleAll }, heldMessages(task))",
    );
    expect(NODE).toContain("performOpen(\n      task,\n      intent,\n      {");
    // And the whole-task POST exists in exactly two places: the shared performer,
    // and the List row's own Mark read button (which stays on the page and awaits
    // it). No third mark-read path.
    expect((VIEWS.match(/markWholeTaskRead\(/g) ?? []).length).toBe(2);
  });

  it("marks the whole thread read, local half first", () => {
    const at = VIEWS.indexOf("function performOpen(");
    const fn = VIEWS.slice(at, VIEWS.indexOf("\n}", at));
    // Guarded by the intent, so an ordinary press on a read task posts nothing —
    // and a task with no session never gets here at all, because there is no
    // intent to spend.
    expect(fn).toContain("if (intent.markRead) {");
    // Local first (the pill has to go on the press, not 20s later), then ONE
    // whole-task request. Never a loop over messages.
    expect(fn.indexOf("marks.clearAll(task, held)")).toBeGreaterThan(
      fn.indexOf("if (intent.markRead) {"),
    );
    expect(fn.indexOf("markWholeTaskRead(task.key)")).toBeGreaterThan(
      fn.indexOf("marks.clearAll(task, held)"),
    );
    expect(fn).not.toContain("markTaskMessageRead");
  });

  it("marks what the CALLER holds, and never reads that off the row itself", () => {
    // The bug this closes: the performer read `task.messages` — the listing's
    // three — so Open chat on a thread expanded to all 89 zeroed the count and
    // left 86 dots with no key that could ever take them back. `held` is now the
    // caller's, required, and the two views hand over what each of them holds.
    const at = VIEWS.indexOf("function performOpen(");
    const sig = VIEWS.slice(at, VIEWS.indexOf("): void {", at));
    expect(sig).toContain("held: TaskMessage[]");
    const fn = VIEWS.slice(at, VIEWS.indexOf("\n}", at));
    expect(fn).not.toContain("task.messages");
    // Both call sites, and both pass the same list to the mark and its rollback.
    expect(NODE).toContain("performOpen(\n      task,\n      intent,");
    expect(NODE.slice(NODE.indexOf("const openChat ="))).toContain("      held,");
    expect(BOARD).toContain(
      "performOpen(task, intent, { clearAll, restoreAll, settleAll }, heldMessages(task))",
    );
    // The List's Open chat and the List's Mark read clear the SAME amount of the
    // same thread: a mark that depended on which button you pressed would be a
    // coin toss, not a rule.
    expect(NODE).toContain("onReadAll(task, held)");
  });

  it("navigates regardless, and never waits on the write", () => {
    const at = VIEWS.indexOf("function performOpen(");
    const fn = VIEWS.slice(at, VIEWS.indexOf("\n}", at));
    // Fire and forget as far as the HOP goes — the press is leaving the page, so
    // a refusal has nobody left to be told and the navigation must not be held up
    // or cancelled by it. But the answer is not thrown away: the mark is settled
    // against it, and a refusal takes it back, so the pill is honestly there
    // again when the reader returns.
    expect(fn).toContain("void markWholeTaskRead(task.key)");
    expect(fn).toContain(".then((answer) => marks.settleAll(task.key, held, answer))");
    expect(fn).toContain(".catch(() => marks.restoreAll(task.key, held))");
    expect(fn).not.toContain("await markWholeTaskRead");
    // The mark is INSIDE the guard and the navigation is OUTSIDE it, so a read
    // task still opens.
    expect(fn.indexOf("navigateUrl(intent.href);")).toBeGreaterThan(
      fn.indexOf("}", fn.indexOf("markWholeTaskRead(task.key)")),
    );
  });

  it("offers nothing on a task with no session, in either view", () => {
    // Both sides gate their gesture on the intent being non-null, so neither can
    // navigate to nowhere and neither marks a thread it never showed.
    expect(CARD).toContain("if (open) onOpen(open);");
    expect(ROW).toContain("{chat && (");
  });

  it("draws the MERGED count on both sides, so the mark goes on the press", () => {
    expect(CARD).toContain("<UnreadDot count={unread} />");
    expect(BOARD).toContain("taskUnread(task, read)");
    // The List asks with the count the row is drawing (local marks included),
    // which is what stops a second press from posting again.
    expect(NODE).toContain("taskUnread(task, read, held)");
    expect(ROW).toContain("<UnreadDot count={unread} />");
  });

  it("keeps the card's actions OUT of the click, by being siblings of it", () => {
    // Archive / Run now sit in a strip pinned over the card, outside the card's
    // own <button>: a press cannot bubble into a button it is not inside, so it
    // neither navigates nor marks.
    const clickAt = CARD.indexOf("if (open) onOpen(open);");
    const actsAt = CARD.indexOf('className="tasks-card-acts"');
    expect(clickAt).toBeGreaterThan(-1);
    expect(actsAt).toBeGreaterThan(clickAt);
    // The card's button closes before the strip opens.
    expect(CARD.lastIndexOf("</button>", actsAt)).toBeGreaterThan(clickAt);
    // And the strip's own buttons ask for the run and the filing, nothing else.
    const acts = CARD.slice(actsAt);
    expect(acts).not.toContain("onOpen");
    expect(acts).toContain("void runNow(run)");
    expect(acts).toContain("void triage(file.status)");
  });

  it("is the row's Open chat button and NOT the row itself", () => {
    // The row's own click TOGGLES the accordion; it does not open a conversation,
    // so there is nothing it could have shown the reader and nothing to mark.
    // Marking a whole task read on a press that merely expands it would clear
    // messages nobody has seen. Read off the row div's OWN handlers, so a comment
    // naming the intent cannot make this pass.
    //
    // Since 2026-08-17 the toggle is guarded rather than bare — a row with one
    // message has nothing to expand — which changes what the press does on a leaf
    // (open its message) but not what it can ever do on an ACCORDION (toggle, and
    // nothing else).
    const gesture = ROW.slice(0, ROW.indexOf('{/* The disclosure gutter'));
    // One handler for the pointer and the keyboard both, so they cannot mean
    // different things — and it is `activate`, whose body is read below.
    expect(gesture).toContain("onClick={activate}");
    expect(gesture).toMatch(/onKeyDown=\{\(e\) => \{[\s\S]*?activate\(\);/);
    // The accordion arm is FIRST, so an expandable row is claimed by the toggle
    // and can never reach the openChat arm below it however many messages it has.
    expect(ACTIVATE).toContain("if (expandable) onToggle();");
    expect(ACTIVATE.indexOf("if (expandable) onToggle();")).toBeLessThan(
      ACTIVATE.indexOf("openChat(chat)"),
    );
    // And it never performs or marks anything itself: the thread arm spends the
    // one shared performer through openChat, which is read in its own block.
    expect(ACTIVATE).not.toContain("performOpen");
    expect(ACTIVATE).not.toContain("markWholeTaskRead");
    // What it MAY do on a leaf row is open that row's single message, and only
    // through the message row's own handler.
    expect(ACTIVATE).toContain("else if (sole) openMessage(sole);");
    // The mark rides on the explicit, named action instead — which keeps its own
    // stopPropagation, so pressing it never also toggles the row.
    const openBtn = ROW.indexOf('title="Open chat"');
    expect(openBtn).toBeGreaterThan(-1);
    const btn = ROW.slice(openBtn);
    expect(btn.indexOf("e.stopPropagation();")).toBeLessThan(
      btn.indexOf("openChat(chat);"),
    );
    // And it no longer navigates behind tasks-lib's back: the bare href is gone
    // from this file entirely, so there is no way left to open a thread without
    // asking the intent first.
    expect(ROW).not.toContain("navigateUrl(href)");
    expect(VIEWS).not.toContain("taskHref(");
  });

  it("gives a per-message dot back when its own write is refused", () => {
    // The concrete-id set was already sound about WHAT it hides (it cannot hide a
    // message it has never named) — what it lacked was the way back, and the
    // comment claiming the next poll restored the dot was wrong for the same
    // reason the whole-task one was: the local entry outranks the poll.
    const at = VIEWS.indexOf("const clear = (taskKey: string, m: TaskMessage)");
    const fn = VIEWS.slice(at, VIEWS.indexOf("\n  };", at));
    expect(fn).toContain("markTaskMessageRead(taskKey, m.message_id).catch");
    expect(fn).toContain("unmarkRead(cur, taskKey, m.message_id)");
  });

  it("leaves the per-message click alone — one turn, one message", () => {
    // A message row lands on its OWN anchor, so it marks that one message. That
    // distinction is the whole reason read state is per message, and neither the
    // card nor Open chat may collapse it.
    expect(VIEWS).toContain("onRead(task.key, m);");
    expect(VIEWS).toContain("markTaskMessageRead(taskKey, m.message_id)");
    const at = VIEWS.indexOf("const openMessage = (m: TaskMessage)");
    const fn = VIEWS.slice(at, VIEWS.indexOf("\n  };", at));
    expect(fn).toContain("messageHref(task, m)");
    expect(fn).not.toContain("markWholeTaskRead");
    expect(fn).not.toContain("onReadAll");
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
  it("gives every lane a list and keeps the server's order on a tie", () => {
    const map = groupByColumn([
      task({ key: "a", status: "done" }),
      task({ key: "b", status: "upcoming" }),
      task({ key: "c", status: "done" }),
    ]);
    // Every lane the board draws, in the board's own order — Failed between
    // Done and Archive.
    expect([...map.keys()]).toEqual(BOARD_COLUMNS.map((c) => c.key));
    expect(map.get("done")!.map((t) => t.key)).toEqual(["a", "c"]);
    expect(map.get("in_progress")).toEqual([]);
    expect(map.get("failed")).toEqual([]);
  });

  it("files a failed task in its own lane, not in Done", () => {
    const map = groupByColumn([task({ key: "x", status: FAILED })]);
    expect(map.get("failed")!.map((t) => t.key)).toEqual(["x"]);
    expect(map.get("done")).toEqual([]);
  });
});

// ---- per-lane order ----------------------------------------------------------
// The Board's ONE exception to "the client keeps the server's order", and the
// reason for it: a lane of future work is read to find out what happens NEXT,
// which is the opposite direction from every lane about the past.

const SEC = (iso: string) => Math.floor(Date.parse(iso) / 1000);

/** A task in one lane, holding exactly the messages given (no fixture ones). */
function laned(key: string, status: Task["status"], messages: TaskMessage[]): Task {
  return task({ key, status, messages, message_count: messages.length });
}

/** A pending run: never ran, so `at` is the only time it has. */
const due = (iso: string, over: Partial<TaskMessage> = {}) =>
  msg({ state: "pending", at: SEC(iso), ran_at: 0, ...over });

/** A run that happened. `ran_at` defaults to `at` — the ordinary, on-time case. */
const ran = (at: string, ranAt: string = at, over: Partial<TaskMessage> = {}) =>
  msg({ state: "sent", at: SEC(at), ran_at: SEC(ranAt), ...over });

const keys = (map: Map<BoardColumn, Task[]>, lane: BoardColumn) =>
  (map.get(lane) ?? []).map((t) => t.key);

describe("nextRunAt / lastRunAt", () => {
  it("takes the EARLIEST pending message as the next run", () => {
    // Newest-first by `at`, as the server sends them: October, then Friday.
    const t = laned("a", "upcoming", [due("2026-10-01T09:00:00"), due("2026-08-21T09:00:00")]);
    expect(nextRunAt(t)).toBe(SEC("2026-08-21T09:00:00"));
  });

  it("takes an OVERDUE pending over a later one — past scheduling is allowed", () => {
    // This branch schedules into the past on purpose and runs missed work on
    // open, so a pending whose `at` has gone by is an ordinary state and it is the
    // work that should run first. Newest-first by `at`, as the server sends it.
    const t = laned("a", "upcoming", [
      due("2026-10-01T09:00:00"),
      ran("2026-08-15T09:00:00"),
      due("2026-08-14T09:00:00"), // overdue: due Friday, still pending
    ]);
    expect(nextRunAt(t)).toBe(SEC("2026-08-14T09:00:00"));
    expect(isPastDue(nextRunAt(t), NOW)).toBe(true);
  });

  it("reads the ROW's next run when the window has hidden the overdue pending", () => {
    // The case the window cannot answer, and the field that answers it. The
    // window is the three newest by `at` (server: tasks.py `_row` sorts the merged
    // thread ascending and keeps the tail), so an overdue pending is pushed out of
    // it by three messages with later `at` — and reading the window alone gave the
    // LATER pending, a bound rather than the next run.
    //
    // `next_run` is `min(at)` over every pending ENTRY, taken on the server before
    // the tail is cut. It is the whole fix, and it is read in preference to the
    // window rather than alongside it.
    const t = task({
      key: "hidden",
      status: "upcoming",
      message_count: 40, // the overdue pending is one of the 37 we do not hold
      next_run: SEC("2026-08-14T09:00:00"),
      next_run_entry: "e-overdue",
      messages: [
        due("2026-10-01T09:00:00"),
        ran("2026-08-15T09:00:00"),
        ran("2026-08-14T09:00:00"),
      ],
    });
    expect(nextRunAt(t)).toBe(SEC("2026-08-14T09:00:00"));
    expect(isPastDue(nextRunAt(t), NOW)).toBe(true);
    // Not the window's answer, which is the point.
    expect(nextRunAt(t)).not.toBe(SEC("2026-10-01T09:00:00"));
  });

  it("falls back to the window on a row with no `next_run` — an older server", () => {
    // The fields are optional and a server that predates them sends neither. The
    // answer is then exactly what it used to be: the later pending, a bound that
    // can be late and never early, and a real place in the lane rather than none.
    const t = task({
      key: "hidden",
      status: "upcoming",
      message_count: 40,
      messages: [
        due("2026-10-01T09:00:00"),
        ran("2026-08-15T09:00:00"),
        ran("2026-08-14T09:00:00"),
      ],
    });
    expect(t.next_run).toBe(undefined);
    expect(nextRunAt(t)).toBe(SEC("2026-10-01T09:00:00"));
    expect(nextRunAt(t)!).toBeGreaterThanOrEqual(SEC("2026-08-10T09:00:00"));
    // And the window still wins over a named run it can see to be EARLIER — the
    // one case that happens, a pending entry with no id, which the server refuses
    // to name because the button could not fire it.
    const idless = task({
      status: "upcoming",
      next_run: SEC("2026-10-01T09:00:00"),
      next_run_entry: "e-oct",
      messages: [due("2026-08-14T09:00:00", { entry_id: "" })],
    });
    expect(nextRunAt(idless)).toBe(SEC("2026-08-14T09:00:00"));
  });

  it("has no next run when the window holds nothing pending", () => {
    expect(nextRunAt(laned("a", "done", [ran("2026-08-16T09:00:00")]))).toBe(null);
    expect(nextRunAt(laned("a", "upcoming", []))).toBe(null);
  });

  it("dates the last run by when it RAN, not by what it was due for", () => {
    // Caught up: due Thursday, actually ran Sunday. `at` is the wrong answer.
    const t = laned("a", "done", [ran("2026-08-13T09:00:00", "2026-08-16T11:00:00")]);
    expect(lastRunAt(t)).toBe(SEC("2026-08-16T11:00:00"));
  });

  it("falls back to `at` for a run with no ran_at, and skips the ones that never ran", () => {
    // A missed one-off never ran, so `at` is the closest time it has — and it
    // is the event the Failed lane exists to show, so it must still have one.
    const missed = laned("a", "failed", [
      msg({ state: "missed", at: SEC("2026-08-15T09:00:00"), ran_at: 0 }),
    ]);
    expect(lastRunAt(missed)).toBe(SEC("2026-08-15T09:00:00"));
    // Pending / cancelled / skipped date no run at all, even though they have
    // an `at` — this is what stops a recurring task's FUTURE occurrence from
    // dragging a settled card to the top of Done.
    for (const state of ["pending", "cancelled", "skipped"] as const) {
      const t = laned("b", "done", [msg({ state, at: SEC("2026-10-01T09:00:00"), ran_at: 0 })]);
      expect(lastRunAt(t)).toBe(null);
    }
    expect(lastRunAt(laned("c", "done", []))).toBe(null);
  });

  it("ignores a future occurrence when dating a settled task's last run", () => {
    const t = laned("a", "done", [
      due("2026-10-01T09:00:00"), // next week's occurrence, not a run
      ran("2026-08-16T10:00:00"),
    ]);
    expect(lastRunAt(t)).toBe(SEC("2026-08-16T10:00:00"));
  });
});

describe("lane order", () => {
  it("puts the SOONEST run at the top of Upcoming — the one ascending lane", () => {
    // Handed over in the server's order (`last_active` descending), which says
    // nothing about what runs next: the October task was touched most recently.
    const tasks = [
      laned("oct", "upcoming", [due("2026-10-01T09:00:00")]),
      laned("friday", "upcoming", [due("2026-08-21T09:00:00")]),
      laned("soon", "upcoming", [due("2026-08-16T12:10:00")]),
      laned("tomorrow", "upcoming", [due("2026-08-17T09:00:00")]),
    ];
    expect(keys(groupByColumn(tasks), "upcoming")).toEqual([
      "soon", "tomorrow", "friday", "oct",
    ]);
    // The exception is the Board's alone: the same list, read by the List, is
    // still exactly what the server sent.
    expect(filterTasks(tasks, EMPTY_FILTERS).map((t) => t.key)).toEqual([
      "oct", "friday", "soon", "tomorrow",
    ]);
  });

  it("puts PAST DUE work ahead of future work, by rule and not by accident", () => {
    const tasks = [
      laned("soon", "upcoming", [due("2026-08-16T12:10:00")]),
      laned("late", "upcoming", [due("2026-08-14T09:00:00")]),
      laned("later", "upcoming", [due("2026-08-20T09:00:00")]),
      laned("latest", "upcoming", [due("2026-10-01T09:00:00")]),
    ];
    expect(keys(groupByColumn(tasks, NOW), "upcoming")).toEqual([
      "late", "soon", "later", "latest",
    ]);
    // And it is a RULE, named on the lane, rather than a side effect of the lane
    // happening to be ascending: ascending puts a past time first anyway, which is
    // exactly why the promise had to stop resting on it. The first person to
    // reconsider `dir` would otherwise have broken "overdue at the top" without
    // touching a line that mentions overdue work.
    expect(LANE_SORTS.upcoming.overdueFirst).toBe(true);
    expect(BOARD_COLUMNS.filter((c) => LANE_SORTS[c.key].overdueFirst).map((c) => c.key))
      .toEqual(["upcoming"]);
  });

  it("orders the overdue bucket most-overdue first — the next one out", () => {
    const tasks = [
      laned("yesterday", "upcoming", [due("2026-08-15T09:00:00")]),
      laned("lastWeek", "upcoming", [due("2026-08-09T09:00:00")]),
      laned("thisMorning", "upcoming", [due("2026-08-16T08:00:00")]),
    ];
    expect(keys(groupByColumn(tasks, NOW), "upcoming")).toEqual([
      "lastWeek", "yesterday", "thisMorning",
    ]);
  });

  it("does not bury an overdue pending behind its OWN later occurrence", () => {
    // Bugbot's case, in the half the window can answer: the task's newest message
    // is next month's occurrence, and two runs sit between it and the overdue
    // pending. The lane must read the overdue one, not the newest.
    const tasks = [
      laned("buried", "upcoming", [
        due("2026-10-01T09:00:00"),
        ran("2026-08-15T09:00:00"),
        due("2026-08-14T09:00:00"),
      ]),
      laned("soon", "upcoming", [due("2026-08-16T12:10:00")]),
    ];
    expect(keys(groupByColumn(tasks, NOW), "upcoming")).toEqual(["buried", "soon"]);
  });

  it("puts a task whose overdue pending is OUTSIDE the window at the HEAD", () => {
    // The gap that used to be pinned here, now the fix. Three messages with later
    // `at` push the overdue pending out of the listing window, so the only pending
    // time the window shows is next month's occurrence — which used to sort this
    // card behind work due in October while the run that should have gone on Friday
    // sat waiting. The row names it (`next_run`), so it leads the lane.
    const hidden = task({
      key: "hidden",
      status: "upcoming",
      message_count: 40,
      next_run: SEC("2026-08-14T09:00:00"),
      next_run_entry: "e-overdue",
      messages: [
        due("2026-10-01T09:00:00"),
        ran("2026-08-15T09:00:00"),
        ran("2026-08-14T09:00:00"),
      ],
    });
    const order = keys(groupByColumn([
      hidden,
      laned("soon", "upcoming", [due("2026-08-16T12:10:00")]),
      laned("timeless", "upcoming", [ran("2026-08-15T09:00:00")]),
    ], NOW), "upcoming");
    // Ahead of "soon", which is not overdue at all: past due comes first.
    expect(order).toEqual(["hidden", "soon", "timeless"]);
    expect(nextRunAt(hidden)).toBe(SEC("2026-08-14T09:00:00"));
    // And the promise the order makes is one the button keeps — the same entry,
    // not the October occurrence the card is still carrying.
    expect(runNowIntent(hidden)!.entryId).toBe("e-overdue");
    expect(dropAction(hidden, "in_progress")).toEqual({
      kind: "run", entryId: "e-overdue", messageId: "",
    });
  });

  it("still sorts a row with no `next_run` from its window — an older server", () => {
    // Nothing regressed for a server that does not send the field: the card is
    // ordered by the later pending it can see, keeps a real place in the lane, and
    // its button fires the message that place was based on.
    const hidden = task({
      key: "hidden",
      status: "upcoming",
      message_count: 40,
      messages: [
        due("2026-10-01T09:00:00", { message_id: "MSG-040", entry_id: "e-oct" }),
        ran("2026-08-15T09:00:00"),
        ran("2026-08-14T09:00:00"),
      ],
    });
    const order = keys(groupByColumn([
      hidden,
      laned("soon", "upcoming", [due("2026-08-16T12:10:00")]),
      laned("timeless", "upcoming", [ran("2026-08-15T09:00:00")]),
    ], NOW), "upcoming");
    expect(order).toEqual(["soon", "hidden", "timeless"]);
    expect(nextRunAt(hidden)).toBe(SEC("2026-10-01T09:00:00"));
    expect(runNowIntent(hidden)!.entryId).toBe("e-oct");
  });

  it("reads `now` once for the whole board, not once per comparison", () => {
    // A comparator that changes its mind halfway through a sort has no defined
    // output. One instant, handed down from groupByColumn to every lane.
    const tasks = [
      laned("a", "upcoming", [due("2026-08-16T11:59:59")]),
      laned("b", "upcoming", [due("2026-08-16T12:00:01")]),
    ];
    expect(keys(groupByColumn(tasks, NOW), "upcoming")).toEqual(["a", "b"]);
    expect(sortLane(tasks, "upcoming", NOW).map((t) => t.key)).toEqual(["a", "b"]);
    // ...and the same list at a later instant is the same list: both are overdue
    // by then, and the order inside the bucket is the same ascending one.
    expect(sortLane(tasks, "upcoming", NOW + 86400000).map((t) => t.key))
      .toEqual(["a", "b"]);
  });

  it("says nothing about being late on a lane that is about the past", () => {
    // Overdue is Upcoming's question. A settled lane sorts by its last run,
    // descending, and every time in it is by definition in the past.
    expect(isPastDue(SEC("2026-08-15T09:00:00"), NOW)).toBe(true);
    expect(isPastDue(SEC("2026-10-01T09:00:00"), NOW)).toBe(false);
    // Null is not late: a task with no time has nothing to be late for.
    expect(isPastDue(null, NOW)).toBe(false);
    const done = [
      laned("old", "done", [ran("2026-08-14T09:00:00")]),
      laned("new", "done", [ran("2026-08-16T10:00:00")]),
    ];
    expect(keys(groupByColumn(done, NOW), "done")).toEqual(["new", "old"]);
  });

  it("orders Upcoming by the earliest pending message, not the newest one", () => {
    const tasks = [
      // Its newest message is October, but it fires on Tuesday.
      laned("both", "upcoming", [due("2026-10-01T09:00:00"), due("2026-08-18T09:00:00")]),
      laned("one", "upcoming", [due("2026-08-20T09:00:00")]),
    ];
    expect(keys(groupByColumn(tasks), "upcoming")).toEqual(["both", "one"]);
  });

  it("puts the most recent RUN at the top of Done, by when it ran", () => {
    const tasks = [
      laned("onTime", "done", [ran("2026-08-16T10:00:00")]),
      // Due Thursday, caught up on Sunday at 11:00 — the newest run of the three.
      laned("caught", "done", [ran("2026-08-13T09:00:00", "2026-08-16T11:00:00")]),
      laned("old", "done", [ran("2026-08-15T09:00:00")]),
    ];
    // By `ran_at`, which is when the work actually happened.
    expect(keys(groupByColumn(tasks), "done")).toEqual(["caught", "onTime", "old"]);
    // Ordering by `at` — what it was DUE for — would have filed Sunday's run
    // two days back, behind work that finished before it. This is that claim,
    // written down so the fallback cannot quietly become the primary key.
    expect(keys(groupByColumn(tasks), "done")).not.toEqual(["onTime", "old", "caught"]);
  });

  it("orders Failed the same way — most recent run first", () => {
    const tasks = [
      laned("broke-old", FAILED, [
        msg({ state: "error", at: SEC("2026-08-14T09:00:00"), ran_at: SEC("2026-08-14T09:01:00") }),
      ]),
      laned("broke-now", FAILED, [
        msg({ state: "error", at: SEC("2026-08-16T09:00:00"), ran_at: SEC("2026-08-16T09:02:00") }),
      ]),
    ];
    expect(keys(groupByColumn(tasks), "failed")).toEqual(["broke-now", "broke-old"]);
  });

  it("puts the most recently started work at the top of In Progress", () => {
    const tasks = [
      laned("earlier", "in_progress", [
        msg({ state: "sending", at: SEC("2026-08-16T09:00:00"), ran_at: SEC("2026-08-16T09:00:00") }),
      ]),
      laned("later", "in_progress", [
        msg({ state: "sending", at: SEC("2026-08-16T11:30:00"), ran_at: SEC("2026-08-16T11:30:00") }),
      ]),
    ];
    expect(keys(groupByColumn(tasks), "in_progress")).toEqual(["later", "earlier"]);
  });

  it("leaves Archive in the server's own order", () => {
    // Deliberately NOT sorted by any time: nobody scans Archive by time-to-run,
    // and it holds cancelled and skipped messages that date no run at all.
    const tasks = [
      laned("first", "archived", [msg({ state: "cancelled", at: SEC("2026-08-10T09:00:00"), ran_at: 0 })]),
      laned("second", "archived", [ran("2026-08-16T11:00:00")]),
      laned("third", "archived", []),
    ];
    expect(keys(groupByColumn(tasks), "archived")).toEqual(["first", "second", "third"]);
  });

  it("keeps a tie in the server's order, in BOTH directions", () => {
    const sameRun = (key: string) => laned(key, "done", [ran("2026-08-16T10:00:00")]);
    const sameDue = (key: string) => laned(key, "upcoming", [due("2026-08-20T09:00:00")]);
    // The point of the test is that reversing the INPUT reverses the output and
    // nothing else: the sort never invents an order of its own for equal keys,
    // so two cards cannot trade places between two polls of the same data.
    expect(keys(groupByColumn([sameRun("a"), sameRun("b")]), "done")).toEqual(["a", "b"]);
    expect(keys(groupByColumn([sameRun("b"), sameRun("a")]), "done")).toEqual(["b", "a"]);
    expect(keys(groupByColumn([sameDue("a"), sameDue("b")]), "upcoming")).toEqual(["a", "b"]);
    expect(keys(groupByColumn([sameDue("b"), sameDue("a")]), "upcoming")).toEqual(["b", "a"]);
  });

  it("re-sorting the same list twice is the same list", () => {
    // Idempotence is what the 20-second poll actually needs: the second render
    // of unchanged data must be identical to the first.
    const tasks = [
      laned("oct", "upcoming", [due("2026-10-01T09:00:00")]),
      laned("soon", "upcoming", [due("2026-08-16T12:10:00")]),
      laned("none", "upcoming", [ran("2026-08-15T09:00:00")]),
    ];
    const once = keys(groupByColumn(tasks), "upcoming");
    expect(keys(groupByColumn(groupByColumn(tasks).get("upcoming")!), "upcoming")).toEqual(once);
  });

  it("sends a task with no usable time to the END of its lane, both directions", () => {
    const upcoming = [
      // First in the server's list, and with nothing pending to be sorted by.
      laned("timeless", "upcoming", [ran("2026-08-15T09:00:00")]),
      laned("empty", "upcoming", []),
      laned("soon", "upcoming", [due("2026-08-16T12:10:00")]),
      laned("later", "upcoming", [due("2026-08-20T09:00:00")]),
    ];
    // Last, not first, even though ascending would otherwise reward a small
    // key — and among themselves in the server's order.
    expect(keys(groupByColumn(upcoming), "upcoming")).toEqual([
      "soon", "later", "timeless", "empty",
    ]);
    const done = [
      laned("nothing", "done", []),
      laned("ran", "done", [ran("2026-08-16T10:00:00")]),
    ];
    expect(keys(groupByColumn(done), "done")).toEqual(["ran", "nothing"]);
  });

  it("never mutates the list it was handed", () => {
    const tasks = [
      laned("oct", "upcoming", [due("2026-10-01T09:00:00")]),
      laned("soon", "upcoming", [due("2026-08-16T12:10:00")]),
    ];
    groupByColumn(tasks);
    expect(tasks.map((t) => t.key)).toEqual(["oct", "soon"]);
  });

  it("names every lane's order exactly once, for every lane the board draws", () => {
    // A lane added to the board without an entry here would fall through to
    // whatever `undefined` sorts as.
    expect(Object.keys(LANE_SORTS).sort()).toEqual(
      BOARD_COLUMNS.map((c) => c.key as string).sort(),
    );
    // The one ascending lane, and the one that sorts nothing.
    const asc = BOARD_COLUMNS.filter((c) => LANE_SORTS[c.key].dir === "asc" &&
      LANE_SORTS[c.key].key !== "server").map((c) => c.key);
    expect(asc).toEqual(["upcoming"]);
    expect(LANE_SORTS.archived.key).toBe("server");
  });

  it("has no time to sort a lane by when the lane keeps the server's order", () => {
    const t = laned("a", "archived", [ran("2026-08-16T10:00:00")]);
    expect(laneTime(t, "archived")).toBe(null);
    expect(laneTime(t, "done")).toBe(SEC("2026-08-16T10:00:00"));
    expect(laneTime(laned("b", "upcoming", [due("2026-08-20T09:00:00")]), "upcoming"))
      .toBe(SEC("2026-08-20T09:00:00"));
  });

  it("sorts one lane on its own, for the lane it is asked about", () => {
    // sortLane is per-lane by construction: the same two tasks, asked as
    // Upcoming and as Done, come back in opposite orders.
    const a = laned("a", "done", [ran("2026-08-16T10:00:00")]);
    const b = laned("b", "done", [ran("2026-08-16T11:00:00")]);
    expect(sortLane([a, b], "done").map((t) => t.key)).toEqual(["b", "a"]);
    expect(sortLane([a, b], "archived").map((t) => t.key)).toEqual(["a", "b"]);
  });
});

// ---- opening a thread --------------------------------------------------------
// Opening a thread clears the unread the gesture was pointing at (Akshil,
// 2026-08-17: "when i click from kanban on unread task it should register it read
// correct?") — and that is a rule about OPENING, not about the Board, so the
// Board card's click and the List row's Open chat button ask this one function
// rather than one each.

describe("openThreadIntent", () => {
  it("opens the thread and marks it read when there is something unread", () => {
    const t = task({ unread: 3 });
    const intent = openThreadIntent(t)!;
    // The same href taskHref gives — the conversation, with no per-turn anchor,
    // which is exactly why the mark is whole-task rather than per message.
    expect(intent.href).toBe(taskHref(t)!);
    expect(intent.markRead).toBe(true);
  });

  it("opens without marking when nothing is unread", () => {
    const intent = openThreadIntent(task({ unread: 0 }))!;
    expect(intent.href).toBe(taskHref(task())!);
    // No POST on an ordinary press.
    expect(intent.markRead).toBe(false);
  });

  it("takes the DISPLAYED count, so a second press posts nothing", () => {
    const t = task({ unread: 3 });
    // What taskUnread returns once this task has been cleared locally.
    expect(openThreadIntent(t, 0)!.markRead).toBe(false);
    expect(openThreadIntent(t, taskUnread(t, markAllRead(new Set(), t)))!.markRead)
      .toBe(false);
  });

  it("does nothing at all for a task with no session — not even the mark", () => {
    // §5: the id is minted on the first run, so there is no conversation to
    // open, and marking a thread read on a press that showed the reader nothing
    // would clear a badge for messages they never saw.
    expect(openThreadIntent(task({ session_id: "", unread: 4 }))).toBe(null);
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

// The "ran 07:12 today" LABEL is gone (Akshil, 2026-08-17: "I don't think I need
// this as well, the RAND Today stuff") — a message row carrying two absolute times
// was the crowding being trimmed. The DISTINCTION it drew is not gone: `at` is what
// was asked for and `ran_at` is when the turn started, they genuinely differ on a
// late or early run, and the tooltip is now the one place that says so. These tests
// keep the data-layer rule and drop only the formatting.
describe("ranOffSchedule", () => {
  const at = (iso: string) => Math.floor(Date.parse(iso) / 1000);

  it("is false when the run and the due time are the same fact", () => {
    expect(ranOffSchedule(msg())).toBe(false);
    // A send is never instantaneous; a few seconds of drift is not news.
    expect(ranOffSchedule(msg({ ran_at: at("2026-08-16T09:00:30") }))).toBe(false);
    // Nothing has run yet.
    expect(ranOffSchedule(msg({ state: "pending", ran_at: 0 }))).toBe(false);
  });

  it("is true for an EARLY run — a task dragged into In Progress", () => {
    // Run-now leaves `due` alone, so 09:00 is still what the row was for, and the
    // tooltip is what admits it went out at 07:12.
    const early = msg({ ran_at: at("2026-08-16T07:12:00") });
    expect(ranOffSchedule(early)).toBe(true);
    expect(messageWhenTitle(early)).toContain("ran ");
    expect(messageWhenTitle(early)).toContain("Scheduled for ");
    // BOTH instants, absolutely — this is the only surviving place a late or early
    // run can be told from an on-time one.
    expect(messageWhenTitle(early)).toBe(
      `Scheduled for ${messageStamp(early.at)} · ran ${messageStamp(early.ran_at)}`,
    );
  });

  it("is true for a LATE run — caught up after the app was shut", () => {
    expect(ranOffSchedule(msg({ ran_at: at("2026-08-17T10:30:00") }))).toBe(true);
  });

  it("leaves the tooltip as the plain stamp when the two agree", () => {
    expect(messageWhenTitle(msg())).toBe(messageStamp(msg().at));
  });

  it("takes no clock at all — it compares two stamps the server wrote", () => {
    // It formatted a relative day while it drew a label ("ran 07:12 TODAY"); with
    // the label gone the current time cannot change the answer, and a signature
    // that still asked for one would be inviting a caller to think it could.
    expect(ranOffSchedule.length).toBe(1);
  });
});

// ---- "30m ago" / "in 2h" -------------------------------------------------------
// The row's one time (Akshil, 2026-08-17: the row ended in `15:53 29 Jul 🗀
// ppt_builder` and "both the folder and the time with the date, they are like too
// much for me to handle"). `now` is a parameter, never the clock, which is the only
// reason these boundaries can be pinned at all.

describe("relativeWhen", () => {
  const NOW_S = Math.floor(NOW / 1000);
  const past = (secs: number) => relativeWhen(NOW_S - secs, NOW);
  const soon = (secs: number) => relativeWhen(NOW_S + secs, NOW);

  it("says ONE unit, with the reference's own abbreviations", () => {
    expect(past(5 * 60)).toBe("5m ago");
    expect(past(3 * 3600)).toBe("3h ago");
    expect(past(2 * 86400)).toBe("2d ago");
    expect(past(35 * 86400)).toBe("1mo ago");
    expect(past(400 * 86400)).toBe("1y ago");
    // Never two units: a month and three days is "1mo ago" and nothing more.
    expect(past(33 * 86400)).toBe("1mo ago");
  });

  it("FLOORS every boundary, in both directions", () => {
    // Under a minute has its own word...
    expect(past(59)).toBe(JUST_NOW);
    expect(past(60)).toBe("1m ago");
    // ...and no unit is ever named before the instant reaches it.
    expect(past(3599)).toBe("59m ago");
    expect(past(3600)).toBe("1h ago");
    expect(past(89 * 60)).toBe("1h ago");
    expect(past(86399)).toBe("23h ago");
    expect(past(86400)).toBe("1d ago");
    expect(past(30 * 86400 - 1)).toBe("29d ago");
    expect(past(30 * 86400)).toBe("1mo ago");
    expect(past(360 * 86400 - 1)).toBe("11mo ago");
    expect(past(360 * 86400)).toBe("1y ago");
    expect(soon(3599)).toBe("in 59m");
    expect(soon(3600)).toBe("in 1h");
    expect(soon(30 * 86400)).toBe("in 1mo");
  });

  it("turns round for a FUTURE instant, because most of Upcoming is one", () => {
    // "5m ago" on a run that has not happened is simply false, and the reference
    // app had no future case to copy.
    expect(soon(5 * 60)).toBe("in 5m");
    expect(soon(2 * 3600)).toBe("in 2h");
    expect(soon(3 * 86400)).toBe("in 3d");
  });

  it("never says JUST NOW about something that has not happened", () => {
    // A run due in 45 seconds is not "just now" — that reads as already done, on
    // exactly the rows (Upcoming) where it would be a lie.
    expect(soon(45)).toBe(IMMINENT);
    expect(IMMINENT).not.toBe(JUST_NOW);
    expect(IMMINENT).toContain("in ");
    // The boundary itself belongs to the past: `now` exactly is "just now", which
    // is the honest reading of an instant that has arrived.
    expect(relativeWhen(NOW_S, NOW)).toBe(JUST_NOW);
  });

  it("prints nothing for no time at all", () => {
    expect(relativeWhen(0, NOW)).toBe("");
  });
});

// ---- when the folder chip is worth drawing -------------------------------------

describe("spansProjects", () => {
  it("is false when every row says the same folder, true when they differ", () => {
    const a = task({ key: "a", project: "/Users/me/news" });
    const b = task({ key: "b", project: "/Users/me/news" });
    const c = task({ key: "c", project: "/Users/me/code" });
    expect(spansProjects([a, b])).toBe(false);
    expect(spansProjects([a, b, c])).toBe(true);
    // One row cannot distinguish itself from anything.
    expect(spansProjects([a])).toBe(false);
    expect(spansProjects([])).toBe(false);
  });

  it("is about the ROWS, so a search narrows it exactly as the filter does", () => {
    const tasks = [
      task({ key: "a", project: "/Users/me/news", title: "Pull today's news" }),
      task({ key: "b", project: "/Users/me/code", title: "Review PRs" }),
    ];
    expect(spansProjects(tasks)).toBe(true);
    // The filter's own value is never consulted: both of these are lists of one
    // project, and both must read the same way.
    expect(spansProjects(filterTasks(tasks, { ...EMPTY_FILTERS, projects: ["/Users/me/news"] })))
      .toBe(false);
    expect(spansProjects(filterTasks(tasks, { ...EMPTY_FILTERS, search: "Review" })))
      .toBe(false);
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
