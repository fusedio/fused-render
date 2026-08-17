// The Tasks page's rules, without a DOM: the accordion's Show-more state, the
// per-message unread bookkeeping, the board's drag legality, filtering, and the
// one ordering promise the client makes (it keeps the server's).
import { describe, expect, it } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import type { Task, TaskMessage } from "@platform/lib/api";
import { BOARD_COLUMNS } from "./schedule-lib";
import {
  EMPTY_FILTERS,
  MESSAGE_ANCHOR_PARAM,
  PREVIEW_MESSAGES,
  UNREAD_COUNT_CAP,
  UNREAD_LABEL,
  archiveIntent,
  basename,
  canCancel,
  canRunNow,
  cancelIntent,
  dayLabel,
  dropAction,
  dropLanes,
  filterTasks,
  firstLine,
  groupByColumn,
  isDraggable,
  isFailedTask,
  isUnread,
  markRead,
  messageHref,
  messageStamp,
  messageTime,
  messageTone,
  messageWhenTitle,
  openMessageHref,
  projectOptions,
  ranNote,
  readKey,
  resendTarget,
  runNowIntent,
  runNowTarget,
  taskColumn,
  taskHref,
  taskRunIntent,
  taskUnread,
  threadView,
  tildePath,
  toggleExpanded,
  triageStatus,
  unreadCount,
  unreadMarker,
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
    const t = { ...three(), unread: 0 };
    expect(taskUnread(t, markRead(new Set<string>(), t.key, "MSG-003"))).toBe(0);
  });

  it("keys reads per task — MSG-001 exists in every thread", () => {
    expect(readKey("a", "MSG-001")).not.toBe(readKey("b", "MSG-001"));
    const t = three();
    const other = markRead(new Set<string>(), "some-other-task", "MSG-003");
    expect(taskUnread(t, other)).toBe(3);
  });

  // The task row's own marker. It stayed at the far right of the row after the
  // message dots moved to the head of theirs, which left one marker in two
  // places (Akshil, 2026-08-17: "for the tasks you didn't bring this on the
  // left side, only for the messages you brought. This looks odd").
  it("draws a task's count as a mark, never as nothing and never as a paragraph", () => {
    expect(unreadCount(0)).toBe(null);
    expect(unreadCount(-1)).toBe(null);
    expect(unreadCount(1)).toEqual({ text: "1", label: "1 unread" });
    expect(unreadCount(5)).toEqual({ text: "5", label: "5 unread" });
    // Two digits still print in full: the pill grows, the rail's centre does not
    // move.
    expect(unreadCount(42)!.text).toBe("42");
  });

  it("caps what the pill PRINTS without capping what it says it means", () => {
    const many = unreadCount(1234)!;
    expect(many.text).toBe(`${UNREAD_COUNT_CAP}+`);
    // The accessible name is the truth, uncapped — the cap is a drawing
    // decision about a 16px slot, not a rounding of the count.
    expect(many.label).toBe("1234 unread");
    expect(unreadCount(UNREAD_COUNT_CAP)!.text).toBe(String(UNREAD_COUNT_CAP));
  });

  it("names the task's count the way it names a message's — for a reader", () => {
    // The message marker announces "Unread"; the task's announces how many.
    // Both are names, neither is a bare glyph.
    expect(unreadCount(3)!.label).toContain(UNREAD_LABEL.toLowerCase());
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
    expect(runNowTarget(t)?.message_id).toBe("MSG-003");
  });

  it("takes the older message when two are due at the same second", () => {
    const t = upcoming([T9, T9]);
    expect(runNowTarget(t)?.message_id).toBe("MSG-001");
  });

  it("is null for a task with nothing pending", () => {
    expect(runNowTarget(task())).toBe(null);
    expect(canRunNow(task())).toBe(false);
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
      expect(intent.messageId).toBe(runNowTarget(t)!.message_id);
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
// Two claims the pure half cannot hold on its own: WHICH END of a row the
// unread mark is at, and whether the task row's rail and its thread's dots are
// one column. Both were the bug, so both are read out of the source rather than
// left to a screenshot.

const SHELL = import.meta.dir;
const VIEWS = readFileSync(join(SHELL, "ScheduleTaskViews.tsx"), "utf8");
const TASKS_CSS = readFileSync(join(SHELL, "../styles/tasks.css"), "utf8");

describe("the unread rail", () => {
  it("draws the TASK row's unread leading, not out by the folder chip", () => {
    const from = VIEWS.indexOf('className={"tasks-row"');
    expect(from).toBeGreaterThan(-1);
    // The row ends where the thread it can open begins.
    const row = VIEWS.slice(from, VIEWS.indexOf("{open && (", from));
    const rail = row.indexOf("<UnreadRail");
    expect(rail).toBeGreaterThan(-1);
    // Before the ring, the id, the title and the folder chip it used to trail.
    expect(rail).toBeLessThan(row.indexOf("<StatusIcon"));
    expect(rail).toBeLessThan(row.indexOf("<IdChip"));
    expect(rail).toBeLessThan(row.indexOf("<IdentityChip"));
  });

  it("leads the board card too, where it also used to trail", () => {
    const head = VIEWS.slice(
      VIEWS.indexOf('<span className="schedule-tv-card-head">'),
      VIEWS.indexOf('<span className="schedule-tv-card-title">'),
    );
    expect(head.indexOf("<UnreadPill")).toBeLessThan(head.indexOf("<StatusIcon"));
  });

  it("puts a task row and its messages in ONE class, so it is one column", () => {
    // Both slots are `.tasks-rail`; the old per-view class is gone.
    expect(VIEWS).not.toContain("tasks-msg-flag");
    expect((VIEWS.match(/className="tasks-rail"/g) ?? []).length).toBeGreaterThan(1);
  });

  it("derives the thread's indent from the rail rather than typing it twice", () => {
    // The rail is placed once (--tasks-rail-x) and the thread reaches it by
    // subtracting a message row's own indent. Hand-tune either side separately
    // and the column bends — which is the bug this round fixed.
    expect(TASKS_CSS).toContain("--tasks-rail-x: calc(");
    expect(TASKS_CSS).toContain(
      "calc(var(--tasks-rail-x) - var(--tasks-msg-indent))",
    );
    // ...and the rail slot itself is one fixed, centred width, which is what
    // puts a 7px dot and a count pill on the same centre line.
    expect(TASKS_CSS).toMatch(/\.tasks-rail\s*\{[^}]*justify-content:\s*center/);
    expect(TASKS_CSS).toMatch(/\.tasks-rail\s*\{[^}]*flex:\s*0 0 var\(--tasks-rail-w\)/);
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

  it("never paints it red — archiving destroys nothing", () => {
    // Cancel's hue is the destructive one and the two are one flick apart; using
    // it here would assert the very thing archiving exists to deny.
    const at = TASKS_CSS.indexOf(".tasks-act--archive:hover");
    expect(at).toBeGreaterThan(-1);
    expect(TASKS_CSS.slice(at, TASKS_CSS.indexOf("}", at))).not.toContain("--error");
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

describe("ranNote", () => {
  const at = (iso: string) => Math.floor(Date.parse(iso) / 1000);

  it("says nothing when the run and the due time are the same fact", () => {
    expect(ranNote(msg(), NOW)).toBe("");
    // A send is never instantaneous; a few seconds of drift is not news.
    expect(ranNote(msg({ ran_at: at("2026-08-16T09:00:30") }), NOW)).toBe("");
    // Nothing has run yet.
    expect(ranNote(msg({ state: "pending", ran_at: 0 }), NOW)).toBe("");
  });

  it("names an EARLY run — a task dragged into In Progress", () => {
    // Run-now leaves `due` alone, so 09:00 is still what the row says it was
    // for, and this is the line that admits it went out at 07:12.
    const early = msg({ ran_at: at("2026-08-16T07:12:00") });
    expect(ranNote(early, NOW)).toBe("ran 07:12 today");
    expect(messageWhenTitle(early, NOW)).toContain("ran ");
  });

  it("names a LATE run — caught up after the app was shut", () => {
    expect(ranNote(msg({ ran_at: at("2026-08-17T10:30:00") }), NOW)).toBe(
      "ran 10:30 tomorrow",
    );
  });

  it("leaves the tooltip as the plain stamp when the two agree", () => {
    expect(messageWhenTitle(msg(), NOW)).toBe(messageStamp(msg().at));
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
