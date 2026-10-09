// The scheduled-message rules, and the watcher that applies them. Every test
// here is a rule the template earned: who owns a fired run, what blocks this
// chat, what "tomorrow" means on the two DST days of the year, which id
// actually reopens the box, and the baseline that stops a reload from
// re-rendering yesterday's turns. The watcher is driven through its
// subscription seam: a scripted `schedule` feed whose `resync` answers with
// the next frame of the fixture, the way the bus answers with a snapshot.
import { describe, expect, test } from "bun:test";
import {
  createScheduleWatcher,
  NOTE_OURS,
  SB_STATES,
  schedDayGap,
  schedFindTask,
  schedIsRepeat,
  schedMsgLine,
  schedPendingHere,
  schedRowName,
  schedRowState,
  schedStopTarget,
  schedWhenText,
  schedWhyLine,
  scheduledRunIsOurs,
  type SchedEntry,
  type SchedTask,
} from "./scheduled";

const entry = (over: Partial<SchedEntry> = {}): SchedEntry => ({
  id: "e1",
  state: "pending",
  ...over,
});

describe("ownership", () => {
  test("with a session on screen, either id naming it counts", () => {
    expect(scheduledRunIsOurs(entry({ session_id: "s1" }), "s1")).toBe(true);
    expect(scheduledRunIsOurs(entry({ claude_session_id: "s1" }), "s1")).toBe(true);
  });

  test("with a session on screen, another conversation's run is not ours", () => {
    // Splicing another conversation's turn into this one would be the page
    // telling a lie about what was said where.
    expect(scheduledRunIsOurs(entry({ session_id: "s2" }), "s1")).toBe(false);
    expect(scheduledRunIsOurs(entry({ claude_session_id: "s2" }), "s1")).toBe(false);
  });

  test("with no session, only a send that resumed nothing can be adopted", () => {
    expect(scheduledRunIsOurs(entry(), "")).toBe(true);
    expect(scheduledRunIsOurs(entry({ session_id: "s9" }), "")).toBe(false);
  });
});

describe("what blocks this chat", () => {
  test("pending messages for this session, soonest first", () => {
    const rows = schedPendingHere(
      [
        entry({ id: "late", session_id: "s1", due: "2026-09-09T14:00:00Z" }),
        entry({ id: "soon", session_id: "s1", due: "2026-09-09T09:00:00Z" }),
        entry({ id: "learned", claude_session_id: "s1", due: "2026-09-09T20:00:00Z" }),
      ],
      "s1",
    );
    expect(rows.map((r) => r.id)).toEqual(["soon", "late", "learned"]);
  });

  test("only PENDING, and only this session", () => {
    const rows = schedPendingHere(
      [
        entry({ id: "sent", session_id: "s1", state: "sent" }),
        entry({ id: "theirs", session_id: "s2" }),
        entry({ id: "mine", session_id: "s1" }),
      ],
      "s1",
    );
    expect(rows.map((r) => r.id)).toEqual(["mine"]);
  });

  test("THE LANDING PAGE IS NEVER BLOCKED — its conversation does not exist yet", () => {
    expect(schedPendingHere([entry({ session_id: "s1" })], "")).toEqual([]);
  });

  test("the soonest is named and the rest are counted", () => {
    expect(schedWhyLine([entry(), entry({ id: "e2" })])).toBe(
      "Blocked — a scheduled message runs in this chat. 1 more after it.",
    );
    expect(schedWhyLine([entry({ template_id: "t1" })])).toBe(
      "Blocked — a repeating message runs in this chat.",
    );
    expect(schedWhyLine([])).toBe("");
    // The chat's own comeback says the rescue and when. It is known by the
    // ENTRY's fixed title, or its fixed prompt when the listing spelled no
    // title — never by the conversation's task row, which keeps the comeback's
    // name for every later message a person schedules into it (Bugbot, #1292).
    const at = new Date(2099, 0, 1, 9, 0);
    const now = new Date(2098, 11, 31, 9, 0);
    const line = "Paused on your usage limit — this chat picks up again by itself 09:00 tomorrow.";
    expect(schedWhyLine([entry({ title: "Continue after usage limit", due: at.toISOString() })], now)).toBe(line);
    expect(
      schedWhyLine(
        [
          entry({
            due: at.toISOString(),
            message:
              "Your usage limit has reset. Continue the task you were working on where it stopped. Do not repeat steps that were already completed.",
          }),
        ],
        now,
      ),
    ).toBe(line);
    expect(schedWhyLine([entry({ due: at.toISOString(), message: "Nightly tidy" })], now)).toBe(
      "Blocked — a scheduled message runs in this chat.",
    );
  });
});

describe("what the stop press spends", () => {
  test("a repeat is recognised by any of its three marks", () => {
    expect(schedIsRepeat(entry({ template_id: "t1" }))).toBe(true);
    expect(schedIsRepeat(entry({ repeats: "0 9 * * *" }))).toBe(true);
    expect(schedIsRepeat(entry({ rule: { kind: "weekly" } }))).toBe(true);
    expect(schedIsRepeat(entry())).toBe(false);
  });

  test("a repeat cancels the TEMPLATE, a one-off cancels itself", () => {
    // Cancelling the occurrence moves the block rather than lifting it:
    // `_materialize` arms the next one the moment this one is skipped.
    expect(schedStopTarget(entry({ id: "occ", template_id: "tmpl" }))).toBe("tmpl");
    expect(schedStopTarget(entry({ id: "one" }))).toBe("one");
    // A cron template with no id of its own to point at falls back to its own.
    expect(schedStopTarget(entry({ id: "cron", repeats: "0 9 * * *" }))).toBe("cron");
  });
});

describe("when it goes", () => {
  const now = new Date(2026, 8, 9, 10, 0, 0); // 2026-09-09 10:00 local

  test("an unparseable stamp says so rather than printing NaN", () => {
    expect(schedWhenText("not a date", now)).toBe("at its scheduled time");
    expect(schedWhenText(undefined, now)).toBe("at its scheduled time");
  });

  test("past due is not a time in the past", () => {
    // A queued message is waiting for the next sweep, so the stamp on it
    // stopped being the answer to "when?".
    expect(schedWhenText(new Date(2026, 8, 9, 9, 0).toISOString(), now)).toBe(
      "any moment now",
    );
  });

  test("24-hour clock, then the relative day word", () => {
    expect(schedWhenText(new Date(2026, 8, 9, 14, 5).toISOString(), now)).toBe("14:05 today");
    expect(schedWhenText(new Date(2026, 8, 10, 14, 0).toISOString(), now)).toBe(
      "14:00 tomorrow",
    );
    const inThree = schedWhenText(new Date(2026, 8, 12, 14, 0).toISOString(), now);
    expect(inThree.startsWith("14:00 ")).toBe(true);
    expect(inThree).not.toContain("tomorrow");
  });

  test("DST-SAFE: the day gap is counted from midnights, not from 86_400_000ms", () => {
    // 2026-03-29 is Europe's spring-forward: that local day is 23 hours long, so
    // a millisecond division puts a 00:30 the next morning in the SAME day.
    const before = new Date(2026, 2, 29, 23, 30);
    const after = new Date(2026, 2, 30, 0, 30);
    expect(schedDayGap(after, before)).toBe(1);
    // And the autumn 25-hour day cannot round the other way either.
    expect(schedDayGap(new Date(2026, 9, 25, 0, 30), new Date(2026, 9, 24, 23, 30))).toBe(1);
    // The label follows the gap, at a minute either side of a DST midnight.
    expect(schedWhenText(after.toISOString(), before)).toBe("00:30 tomorrow");
  });
});

describe("the row's name and state", () => {
  test("whitespace COLLAPSES rather than the first line winning", () => {
    expect(schedMsgLine(entry({ message: "Read the following and:\n\n  do it" }))).toBe(
      "Read the following and: do it",
    );
    expect(schedMsgLine(entry())).toBe("A scheduled message");
    expect(schedMsgLine(entry({ message: "   " }))).toBe("A scheduled message");
  });

  test("three ways to the task row, and the message scan is always last", () => {
    const tasks: SchedTask[] = [
      { key: "other", messages: [{ entry_id: "e1" }] },
      { key: "pending:e1" },
      { key: "s1" },
    ];
    // The pending key has priority over the session key (Bugbot, 2026-09-17):
    // when a message is sent to a different folder's task, the entry gets a
    // pending:<entry_id> key, not the session key. Without this, schedFindTask
    // returns an old done task with the same sessionId.
    expect(schedFindTask(tasks, entry(), "s1")?.key).toBe("pending:e1");
    expect(schedFindTask([{ key: "s1" }, { key: "pending:e1" }], entry(), "s1")?.key).toBe(
      "pending:e1",
    );
    // Keyed by the entry when it has no session yet.
    expect(schedFindTask(tasks, entry(), "")?.key).toBe("pending:e1");
    // The message scan is LAST because the listing carries only each task's
    // three newest messages, so it is the one that can legitimately miss — a
    // task whose message names this entry loses to a KEY hit behind it.
    expect(schedFindTask(tasks, entry(), "s0")?.key).toBe("pending:e1");
    expect(schedFindTask([tasks[0]], entry(), "s1")?.key).toBe("other");
    expect(schedFindTask([{ key: "nope" }], entry(), "s1")).toBeNull();
  });

  test("an unknown status is Upcoming, and `failed` wins both hue and label", () => {
    expect(schedRowState(null)).toEqual({ state: "upcoming", label: "Upcoming" });
    expect(schedRowState({ key: "k", status: "wat" })).toEqual({
      state: "upcoming",
      label: "Upcoming",
    });
    expect(schedRowState({ key: "k", status: "in_progress" })).toEqual({
      state: "in_progress",
      label: SB_STATES.in_progress,
    });
    expect(schedRowState({ key: "k", status: "in_progress", failed: true })).toEqual({
      state: "failed",
      label: "Failed",
    });
  });

  test("THE ENTRY IS THE SUBJECT: a pending blocker is Upcoming inside a done task", () => {
    // P4R1-3, five reproductions on both stacks: `/api/tasks` answers `done` for
    // a task holding a finished run AND a future pending message — correct for
    // the board, and a false caption over a composer the pending message is
    // holding shut ("Done · 13:26 today").
    expect(schedRowState({ key: "k", status: "done", failed: false }, { id: "e1", state: "pending" })).toEqual({
      state: "upcoming",
      label: "Upcoming",
    });
    // ...and `failed` on the TASK cannot be reported as this entry's verdict:
    // a pending entry has no verdict to be a failure.
    expect(schedRowState({ key: "k", status: "done", failed: true }, { id: "e1", state: "pending" })).toEqual({
      state: "upcoming",
      label: "Upcoming",
    });
    // A fired entry — the window between the run going away and the block
    // lifting — is the Tasks page's own word for running.
    expect(schedRowState({ key: "k", status: "done" }, { id: "e1", state: "sent" })).toEqual({
      state: "in_progress",
      label: SB_STATES.in_progress,
    });
    // No entry, or a state this bundle does not know: the listing row, whole.
    expect(schedRowState({ key: "k", status: "done", failed: true }, null)).toEqual({
      state: "failed",
      label: "Failed",
    });
    expect(schedRowState({ key: "k", status: "done" }, { id: "e1", state: "wat" })).toEqual({
      state: "done",
      label: SB_STATES.done,
    });
  });

  test("the row names the MESSAGE that is coming, and the title only in its absence", () => {
    // FIX-C: `rec.title` names the CONVERSATION, from some older message. The
    // banner's question is "what is about to run?".
    expect(schedRowName({ id: "e1", message: "run the report" }, { key: "k", title: "Nightly tidy" })).toBe(
      "run the report",
    );
    // Collapsed the way `schedMsgLine` collapses it — the cell is one line.
    expect(schedRowName({ id: "e1", message: "  two\n\nlines  " }, null)).toBe("two lines");
    // No message of its own: the title, then the last resort.
    expect(schedRowName({ id: "e1" }, { key: "k", title: "Nightly tidy" })).toBe("Nightly tidy");
    expect(schedRowName({ id: "e1", message: "   " }, { key: "k" })).toBe("A scheduled message");
    expect(schedRowName(null)).toBe("");
  });
});

// ---- the poller ------------------------------------------------------------

function harness(
  over: {
    entries?: SchedEntry[][];
    sessionId?: string;
    inChat?: boolean;
    busy?: () => boolean;
    fail?: boolean;
    /** Run ids the CONTROLLER has already taken (its own send, a `run` param,
     *  a turn the 5 s standing watch adopted). */
    shown?: Set<string>;
  } = {},
) {
  const notes: string[] = [];
  const resumed: string[] = [];
  const runParams: string[] = [];
  const blockers: SchedEntry[][] = [];
  let pass = 0;
  let frame: ((snap: { entries?: SchedEntry[] } | null, meta: { error?: string }) => void) | null = null;
  let subscribed = 0;
  /** One frame of the fixture, the way the bus answers a subscribe or a resync. */
  const push = () => {
    if (!frame) return;
    if (over.fail) {
      frame(null, { error: "offline" });
      return;
    }
    const feed = over.entries || [];
    frame({ entries: feed[Math.min(pass++, feed.length - 1)] || [] }, {});
  };
  const watcher = createScheduleWatcher({
    file: "/proj",
    subscribe: (cb) => {
      subscribed += 1;
      frame = cb;
      return () => {
        subscribed -= 1;
        if (frame === cb) frame = null;
      };
    },
    resync: push,
    sessionId: () => over.sessionId ?? "s1",
    inChat: () => over.inChat !== false,
    busy: over.busy || (() => false),
    onBlockers: (rows) => blockers.push(rows),
    addNote: (t) => notes.push(t),
    setRunParam: (id) => runParams.push(id),
    resumeRun: (id) => {
      resumed.push(id);
      return Promise.resolve();
    },
    shownRun: (id) => !!over.shown && over.shown.has(id),
  });
  // Started, so the subscription is open; the first frame is the one `tick()`
  // asks for, as a page's first snapshot is.
  const stop = watcher.start();
  return { watcher, notes, resumed, runParams, blockers, stop, push, subscribed: () => subscribed };
}

const fired = (over: Partial<SchedEntry>): SchedEntry =>
  entry({ target: "/proj", run_id: "r-" + over.id, ...over });

describe("the schedule watcher", () => {
  test("FAILS OPEN: an unreadable schedule blocks nothing", async () => {
    const h = harness({ fail: true });
    await h.watcher.tick();
    expect(h.blockers).toEqual([[]]);
    expect(h.notes).toEqual([]);
  });

  test("the first pass is a SILENT BASELINE, at load and not one interval later", async () => {
    const already = [fired({ id: "old", session_id: "s1" })];
    const h = harness({ entries: [already, already] });
    await h.watcher.tick();
    // Everything already recorded happened before this frame existed, and the
    // transcript restore has accounted for the ones that belong here.
    expect(h.notes).toEqual([]);
    expect(h.resumed).toEqual([]);
    expect(h.watcher.baselined()).toBe(true);
    await h.watcher.tick();
    expect(h.resumed).toEqual([]);
  });

  test("a CHAT-ORIGIN run (queued or Force-started send) attaches with no note", async () => {
    const h = harness({
      entries: [[], [fired({ id: "new", session_id: "s1", origin: "chat" })]],
    });
    await h.watcher.tick();
    await h.watcher.tick();
    // The reader's own bubble is finally going; there is nothing to announce.
    expect(h.notes).toEqual([]);
    expect(h.runParams).toEqual(["r-new"]);
    expect(h.resumed).toEqual(["r-new"]);
  });

  test("a run that fires AFTER the baseline is announced, put on the URL and streamed", async () => {
    const h = harness({
      entries: [[], [fired({ id: "new", session_id: "s1" })]],
    });
    await h.watcher.tick();
    await h.watcher.tick();
    expect(h.notes).toEqual([NOTE_OURS]);
    expect(h.runParams).toEqual(["r-new"]);
    expect(h.resumed).toEqual(["r-new"]);
    // Marked, so the next tick does not attach it twice.
    await h.watcher.tick();
    expect(h.resumed).toEqual(["r-new"]);
  });

  test("a foreign run is SILENT here (owner E2E R1, F9) and never marked attached", async () => {
    const rows = [fired({ id: "theirs", session_id: "s2" })];
    const h = harness({ entries: [[], rows, rows] });
    await h.watcher.tick();
    await h.watcher.tick();
    await h.watcher.tick();
    // Another conversation's scheduled run is not this chat's business.
    expect(h.notes).toEqual([]);
    expect(h.resumed).toEqual([]);
    // NOT in `attached`: switching to that session must still restore the turn
    // from history rather than being written off as already handled.
    expect(h.watcher.attached.has("r-theirs")).toBe(false);
    expect(h.watcher.noted.has("r-theirs")).toBe(true);
  });

  test("a live turn owns the transcript: the entry is left UNMARKED for the next tick", async () => {
    const rows = [fired({ id: "new", session_id: "s1" })];
    let busy = true;
    const h = harness({ entries: [[], rows, rows], busy: () => busy });
    await h.watcher.tick();
    await h.watcher.tick();
    expect(h.notes).toEqual([]);
    expect(h.watcher.attached.has("r-new")).toBe(false);
    busy = false;
    await h.watcher.tick();
    expect(h.resumed).toEqual(["r-new"]);
  });

  test("A RUN THE CONTROLLER HAS SHOWN IS ATTACHED, NOT RESUMED", async () => {
    // The standing watch looks every 5 s and this poll every 15, so the watch
    // adopts a fired scheduled run first and streams the turn itself. This
    // poller must write the entry off rather than re-attaching once the turn
    // ends — that second attach is the duplicate turn (Bugbot PR #1075).
    const rows = [fired({ id: "new", session_id: "s1" })];
    const shown = new Set<string>();
    let busy = false;
    const h = harness({ entries: [[], rows, rows], busy: () => busy, shown });
    await h.watcher.tick();
    // The watch gets there first: it owns the id and the turn is in flight.
    shown.add("r-new");
    busy = true;
    await h.watcher.tick();
    expect(h.resumed).toEqual([]);
    // Marked on the spot, so the tick AFTER the turn ends does not attach to it.
    expect(h.watcher.attached.has("r-new")).toBe(true);
    busy = false;
    await h.watcher.tick();
    expect(h.resumed).toEqual([]);
    // And no note: the turn is on screen, and "running now" would describe a
    // turn that has already finished.
    expect(h.notes).toEqual([]);
    expect(h.runParams).toEqual([]);
  });

  test("a run the controller has NOT shown is still this poller's to attach", async () => {
    const rows = [fired({ id: "new", session_id: "s1" })];
    const h = harness({ entries: [[], rows], shown: new Set(["r-other"]) });
    await h.watcher.tick();
    await h.watcher.tick();
    expect(h.resumed).toEqual(["r-new"]);
    expect(h.notes).toEqual([NOTE_OURS]);
  });

  test("only runs for THIS target, and only those with a run id", async () => {
    const h = harness({
      entries: [
        [],
        [
          fired({ id: "elsewhere", session_id: "s1", target: "/other" }),
          entry({ id: "unfired", session_id: "s1", target: "/proj" }),
        ],
      ],
    });
    await h.watcher.tick();
    await h.watcher.tick();
    expect(h.resumed).toEqual([]);
  });

  test("the block is applied on the LANDING page's tick too, and nothing is rendered", async () => {
    const h = harness({
      entries: [[fired({ id: "new", session_id: "s1" })]],
      inChat: false,
      sessionId: "",
    });
    await h.watcher.tick();
    // A block is a fact about the schedule, not about what this frame rendered —
    // so `onBlockers` runs before the home-view return (and answers `[]` here,
    // because a landing page has no conversation to be pending in).
    expect(h.blockers).toEqual([[]]);
    expect(h.watcher.baselined()).toBe(false);
  });

  test("a REPLACED transcript drops both sets, unblocks, and re-polls", async () => {
    const rows = [fired({ id: "new", session_id: "s1" })];
    const h = harness({ entries: [[], rows, rows, rows] });
    await h.watcher.tick();
    await h.watcher.tick();
    expect(h.resumed).toEqual(["r-new"]);
    h.watcher.resetForNewTranscript();
    // The reset baselines again on its own tick, so the run history just
    // restored is not attached over the top of itself.
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
    expect(h.watcher.attached.has("r-new")).toBe(true);
    expect(h.resumed).toEqual(["r-new"]);
  });
});

describe("the subscription (D3: no lap, no rate)", () => {
  test("start() opens ONE subscription, stop() closes it, and a frame after stop is ignored", async () => {
    const h = harness({ entries: [[fired({ id: "a" })]] });
    expect(h.subscribed()).toBe(1);
    h.stop();
    expect(h.subscribed()).toBe(0);
    h.push();
    await Promise.resolve();
    expect(h.blockers).toEqual([]);
  });

  test("a pushed snapshot is a pass — nobody has to ask", async () => {
    // The server pushes on every change; the watcher never decides when to
    // look. Two pushes: the baseline, then the run that fires after it.
    const h = harness({ entries: [[], [fired({ id: "a", session_id: "s1" })]] });
    h.push();
    await Promise.resolve();
    expect(h.watcher.baselined()).toBe(true);
    h.push();
    for (let i = 0; i < 4; i++) await Promise.resolve();
    expect(h.resumed).toEqual(["r-a"]);
    expect(h.runParams).toEqual(["r-a"]);
  });

  test("a refused frame fails OPEN and does not count as the baseline", async () => {
    const h = harness({ entries: [[fired({ id: "a" })]], fail: true });
    await h.watcher.tick();
    expect(h.blockers).toEqual([[]]);
    expect(h.watcher.baselined()).toBe(false);
  });

  test("tick() is one resync, never a fetch; resetForNewTranscript spends one too", async () => {
    let resyncs = 0;
    const watcher = createScheduleWatcher({
      file: "/proj",
      subscribe: () => () => {},
      resync: () => {
        resyncs += 1;
      },
      sessionId: () => "s1",
      inChat: () => true,
      busy: () => false,
      onBlockers: () => {},
      addNote: () => {},
      setRunParam: () => {},
      resumeRun: () => Promise.resolve(),
      shownRun: () => false,
    });
    // A watcher nobody started asks for nothing.
    await watcher.tick();
    expect(resyncs).toBe(0);
    const stop = watcher.start();
    await watcher.tick();
    expect(resyncs).toBe(1);
    watcher.resetForNewTranscript();
    expect(resyncs).toBe(2);
    stop();
    await watcher.tick();
    expect(resyncs).toBe(2);
  });
});
