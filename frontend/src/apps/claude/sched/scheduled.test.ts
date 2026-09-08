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
  schedDayGap,
  schedFindTask,
  schedIsRepeat,
  schedMsgLine,
  schedPendingHere,
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
});

// ---- the poller ------------------------------------------------------------

function harness(
  over: {
    entries?: SchedEntry[][];
    sessionId?: string;
    inChat?: boolean;
    busy?: () => boolean;
    fail?: boolean;
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
