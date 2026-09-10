// The scheduled-message rules, and the poller that applies them. Every test
// here is a rule the template earned: who owns a fired run, what blocks this
// chat, what "tomorrow" means on the two DST days of the year, which id
// actually reopens the box, and the baseline that stops a reload from
// re-rendering yesterday's turns.
import { describe, expect, test } from "bun:test";
import {
  createScheduleWatcher,
  NOTE_FOREIGN,
  NOTE_OURS,
  SB_STATES,
  SCHEDULE_IMMINENT_MS,
  SCHEDULE_POLL_BLOCKED_MS,
  SCHEDULE_POLL_MS,
  schedDayGap,
  schedFindTask,
  schedImminent,
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
    // The two KEY hits are equals and the listing's own order decides between
    // them (T:16985 returns on the first of either) — what the ordering rule
    // buys is that neither is ever beaten by the scan.
    expect(schedFindTask(tasks, entry(), "s1")?.key).toBe("pending:e1");
    expect(schedFindTask([{ key: "s1" }, { key: "pending:e1" }], entry(), "s1")?.key).toBe(
      "s1",
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
  const watcher = createScheduleWatcher({
    file: "/proj",
    fetchSchedule: () => {
      if (over.fail) return Promise.reject(new Error("offline"));
      const feed = over.entries || [];
      const entries = feed[Math.min(pass++, feed.length - 1)] || [];
      return Promise.resolve({ entries });
    },
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
  return { watcher, notes, resumed, runParams, blockers };
}

const fired = (over: Partial<SchedEntry>): SchedEntry =>
  entry({ target: "/proj", run_id: "r-" + over.id, ...over });

describe("pollScheduledRuns", () => {
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

  test("a foreign run is noted ONCE and never marked attached", async () => {
    const rows = [fired({ id: "theirs", session_id: "s2" })];
    const h = harness({ entries: [[], rows, rows] });
    await h.watcher.tick();
    await h.watcher.tick();
    await h.watcher.tick();
    expect(h.notes).toEqual([NOTE_FOREIGN]);
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

describe("the poll's two rates (FIX-D)", () => {
  /** The `setInterval` seam, recorded: `armed` is the sequence of intervals the
   *  watcher has asked for, in order, and `fire` runs whichever is current. */
  function timed(feed: SchedEntry[][], fails?: () => boolean) {
    const armed: number[] = [];
    const cleared: unknown[] = [];
    const live: { fn: (() => void) | null } = { fn: null };
    let pass = 0;
    // A held clock, so "is this blocker imminent" is a fact about the fixture
    // and not about the minute the suite happens to run in.
    const clock = { now: T0 };
    const watcher = createScheduleWatcher({
      file: "/proj",
      fetchSchedule: () =>
        fails?.()
          ? Promise.reject(new Error("offline"))
          : Promise.resolve({ entries: feed[Math.min(pass++, feed.length - 1)] || [] }),
      sessionId: () => "s1",
      inChat: () => true,
      busy: () => false,
      onBlockers: () => {},
      addNote: () => {},
      setRunParam: () => {},
      resumeRun: () => Promise.resolve(),
      shownRun: () => false,
      setInterval: (fn, ms) => {
        armed.push(ms);
        live.fn = fn;
        return armed.length;
      },
      clearInterval: (h) => cleared.push(h),
      now: () => clock.now,
    });
    return { watcher, armed, cleared, clock, fire: () => live.fn?.() };
  }

  const settle = async () => {
    for (let i = 0; i < 6; i++) await Promise.resolve();
  };
  const T0 = Date.parse("2026-09-10T12:00:00Z");
  /** A pending blocker for THIS session, due `ms` from the held clock. */
  const blocker = (id: string, ms: number) =>
    entry({ id, session_id: "s1", due: new Date(T0 + ms).toISOString() });
  /** Due next century — a real blocker, and nothing to hurry for. */
  const far = (id: string) => blocker(id, 40 * 365 * 24 * 3600_000);
  /** Due in a minute — inside SCHEDULE_IMMINENT_MS. */
  const soon = (id: string) => blocker(id, 60_000);

  test("the SLOW rate is armed first, so an open composer never pays for the fast one", async () => {
    const t = timed([[]]);
    t.watcher.start();
    await settle();
    // One interval, at 15 s, and no tear-down: the answer never changed shape.
    expect(t.armed).toEqual([SCHEDULE_POLL_MS]);
    expect(t.cleared).toEqual([]);
  });

  test("an IMMINENT blocker switches the interval to the fast rate, and letting go switches it back", async () => {
    // Blocked, then blocked again (same shape — no churn), then clear.
    const t = timed([[soon("e1")], [soon("e1")], []]);
    t.watcher.start();
    await settle();
    expect(t.armed).toEqual([SCHEDULE_POLL_MS, SCHEDULE_POLL_BLOCKED_MS]);
    // The rate ALREADY armed is not torn down and put back up on every tick.
    t.fire();
    await settle();
    expect(t.armed).toEqual([SCHEDULE_POLL_MS, SCHEDULE_POLL_BLOCKED_MS]);
    // ...and the tick that publishes an empty list goes back to 15 s.
    t.fire();
    await settle();
    expect(t.armed).toEqual([
      SCHEDULE_POLL_MS,
      SCHEDULE_POLL_BLOCKED_MS,
      SCHEDULE_POLL_MS,
    ]);
    // Two switches, two timers cleared — never a second live interval.
    expect(t.cleared.length).toBe(2);
  });

  test("A SCHEDULE THAT CANNOT BE READ BLOCKS NOTHING, and does not leave the fast rate on", async () => {
    let fail = false;
    const t = timed([[soon("e1")]], () => fail);
    t.watcher.start();
    await settle();
    expect(t.armed).toEqual([SCHEDULE_POLL_MS, SCHEDULE_POLL_BLOCKED_MS]);
    // Failing open publishes `[]` — so the rate has to come back down with it,
    // or an offline page polls three times a second for the life of the tab.
    fail = true;
    t.fire();
    await settle();
    expect(t.armed).toEqual([
      SCHEDULE_POLL_MS,
      SCHEDULE_POLL_BLOCKED_MS,
      SCHEDULE_POLL_MS,
    ]);
  });

  test("a bare tick arms nothing at all, and the stop stops the switching too", async () => {
    // `tick` is public and `resetForNewTranscript` calls it; neither may put a
    // timer up behind a watcher nobody started, or after the stop.
    const t = timed([[soon("e1")]]);
    await t.watcher.tick();
    await settle();
    expect(t.armed).toEqual([]);
    const stop = t.watcher.start();
    await settle();
    expect(t.armed).toEqual([SCHEDULE_POLL_MS, SCHEDULE_POLL_BLOCKED_MS]);
    stop();
    await t.watcher.tick();
    await settle();
    expect(t.armed).toEqual([SCHEDULE_POLL_MS, SCHEDULE_POLL_BLOCKED_MS]);
  });

  test("A BLOCKER DUE NEXT CENTURY KEEPS THE SLOW RATE (M1)", async () => {
    // The composer is shut either way — but a chat holding a message scheduled
    // days out must not ask twenty times a minute for the life of the tab. The
    // fast rate buys back the 8-16 s dead composer AFTER a run, and that gap
    // does not exist until the entry is about to fire.
    const t = timed([[far("e1")], [far("e1")]]);
    t.watcher.start();
    await settle();
    expect(t.armed).toEqual([SCHEDULE_POLL_MS]);
    t.fire();
    await settle();
    // Still one interval, still 15 s, and nothing torn down.
    expect(t.armed).toEqual([SCHEDULE_POLL_MS]);
    expect(t.cleared).toEqual([]);
  });

  test("...and it EARNS the fast rate as its due time comes round", async () => {
    // Same entry, same list shape, three laps: far → imminent → past due. The
    // rate is a fact about the clock, so it moves without the blockers moving.
    const t = timed([[blocker("e1", 10 * 60_000)]]);
    t.watcher.start();
    await settle();
    expect(t.armed).toEqual([SCHEDULE_POLL_MS]);
    // Nine minutes on: one minute out, inside the window.
    t.clock.now = T0 + 9 * 60_000;
    t.fire();
    await settle();
    expect(t.armed).toEqual([SCHEDULE_POLL_MS, SCHEDULE_POLL_BLOCKED_MS]);
    // Past due and still pending (the claim sweep has not run): that is the
    // state the fast poll exists for, so it stays fast rather than churning.
    t.clock.now = T0 + 11 * 60_000;
    t.fire();
    await settle();
    expect(t.armed).toEqual([SCHEDULE_POLL_MS, SCHEDULE_POLL_BLOCKED_MS]);
    expect(t.cleared.length).toBe(1);
  });

  test("the imminence rule itself: soonest wins, past due counts, unreadable hurries", () => {
    const at = (ms: number) => new Date(T0 + ms).toISOString();
    expect(schedImminent([], T0)).toBe(false);
    expect(schedImminent(null, T0)).toBe(false);
    // Exactly on the boundary is imminent; one ms past it is not.
    expect(schedImminent([entry({ due: at(SCHEDULE_IMMINENT_MS) })], T0)).toBe(true);
    expect(schedImminent([entry({ due: at(SCHEDULE_IMMINENT_MS + 1) })], T0)).toBe(false);
    // Overdue is the ordinary shape here, and the one the release is nearest in.
    expect(schedImminent([entry({ due: at(-3600_000) })], T0)).toBe(true);
    // A far-future entry beside an imminent one is still an imminent list.
    expect(schedImminent([far("a"), soon("b")], T0)).toBe(true);
    // A stamp we cannot read hurries rather than stranding a shut composer
    // behind a slow poll.
    expect(schedImminent([entry({ due: "not a date" })], T0)).toBe(true);
    expect(schedImminent([entry({})], T0)).toBe(true);
  });
});
