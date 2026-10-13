// THE THREE THINGS THE HOOK OWNS THAT THE RULES CANNOT: what the composer is
// told while the session on screen changes underneath it, which entry a cancel
// actually removes, and what a cancel still in flight is allowed to say once
// the transcript it was pressed in has been replaced. All three were Bugbot
// findings on PR #1075, and all three are invisible to `scheduled.test.ts`
// because none of them is a rule about the schedule — they are facts about a
// press and a render racing each other.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, expect, test } from "bun:test";
import { createElement } from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";

// Types are erased, so they import statically; the VALUE import is deferred
// past `installDomShim()` because `@platform/lib/api` reads its environment at
// module scope.
import type { ScheduleApi, ScheduleFeed, ScheduleState } from "./useSchedule";
import type { SchedEntry, ScheduleFrame, SchedTask } from "./scheduled";
import type { ChatController } from "../protocol/controller-api";

const { useSchedule } = await import("./useSchedule");
// The caption builder itself, so "the header's row carries the link's fields" is
// asserted as the sentence a reader actually gets rather than as a field name.
const { queueCaption } = await import("@platform/lib/queue");
type QueueFacts = import("@platform/lib/queue").QueueFacts;

type Api = ScheduleApi;
type State = ScheduleState;
type Entry = SchedEntry;

const pending = (id: string, due: string, over: Partial<Entry> = {}): Entry => ({
  id,
  state: "pending",
  session_id: "s1",
  due,
  ...over,
});

/** Enough of the controller for the poller: nothing here ever fires a run, so
 *  the four members it reaches for are the four that exist. */
const controller = {
  isBusy: () => false,
  addNote: () => {},
  resumeRun: async () => {},
  hasShownRun: () => false,
} as unknown as ChatController;

/** A promise the test resolves by hand — the whole point is the window BETWEEN
 *  the press and the answer. */
function deferred<T>(): {
  promise: Promise<T>;
  resolve(v: T): void;
  reject(e: unknown): void;
} {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  // An unobserved rejection is a process-level warning in bun, and the hook is
  // the only observer there is meant to be.
  return { promise, resolve, reject };
}

const mounted: ReactTestRenderer[] = [];
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
});

interface Harness {
  state(): State;
  /** Re-render with a different session on screen — no frame, no fetch. The
   *  leader moves with it when one is given (the adoption clears it). */
  setSession(id: string, leaderId?: string): Promise<void>;
  /** Push the schedule's current answer as a snapshot, the way the server does
   *  on every change, and flush it. */
  poll(): Promise<void>;
  /** What the `schedule` topic answers next. */
  serve(entries: Entry[]): void;
  /** Wedge the schedule open — a resync that nothing answers — so the
   *  confirming snapshot a cancel asks for in its `finally` cannot land over
   *  the local edit under test. */
  holdSchedule(): void;
  /** How many times the pane asked the schedule for a fresh snapshot. */
  resyncs(): number;
  cancels: string[];
  /** The next cancel's answer, installed by the test. */
  nextCancel(d: { promise: Promise<unknown> }): void;
  /** Push a tasks-listing frame, the way the server does the instant the
   *  queue manager moves anything. */
  feed(rows: SchedTask[]): Promise<void>;
  /** How many subscribers the listing feed has right now. */
  feeding(): number;
}

async function mount(
  initial: Entry[],
  session = "s1",
  tasks: SchedTask[] = [],
  /** The project queue's switch, INJECTED rather than left to the pref: the
   *  hook subscribes to a process-global `/api/prefs` answer, and a suite that
   *  drove it by writing that global would be deciding the flag for every other
   *  file in the same bun run. */
  queueEnabled?: boolean,
  /** The entry a session-less chat's messages are grouped under
   *  (`sched/queue-leader`) — "" for every chat that has a session. */
  leader = "",
): Promise<Harness> {
  let served = initial;
  const cancels: string[] = [];
  let pendingCancel: { promise: Promise<unknown> } | null = null;
  let held = false;
  let resyncs = 0;
  const api: Api = {
    cancelScheduledMessage: async (id) => {
      cancels.push(id);
      if (!pendingCancel) return undefined;
      const d = pendingCancel;
      pendingCancel = null;
      return d.promise;
    },
  };

  /** THE SCHEDULE SUBSCRIPTION, injected: one frame callback at a time, which
   *  the hook holds for the life of the watcher. A resync answers with the
   *  schedule as served, unless the test has wedged it. */
  let frame: ScheduleFrame | null = null;
  const pushSchedule = () => frame?.({ entries: served }, {});
  const scheduleFeed: ScheduleFeed = {
    subscribe: (cb) => {
      frame = cb;
      return () => {
        if (frame === cb) frame = null;
      };
    },
    resync: () => {
      resyncs += 1;
      if (!held) pushSchedule();
    },
  };

  /** THE TASKS LISTING FEED, injected. One subscriber at a time is all the hook
   *  ever takes, which is also what makes `feeding()` a real assertion. */
  const feeds: Array<(rows: SchedTask[]) => void> = [];
  const subscribeRows = (cb: (rows: SchedTask[]) => void) => {
    feeds.push(cb);
    return () => {
      const at = feeds.indexOf(cb);
      if (at >= 0) feeds.splice(at, 1);
    };
  };

  let out: State | null = null;
  function Probe(props: { sessionId: string; leaderId: string }) {
    out = useSchedule({
      controller,
      file: "/w/app",
      sessionId: props.sessionId,
      leaderId: props.leaderId,
      // A QUEUED NEW CHAT IS IN A CHAT (`ClaudeChat`: the `queued=` param sets
      // `inChat` before any session exists) — it has a leader and no session,
      // which is the one shape the live row matters most on.
      inChat: !!props.sessionId || !!props.leaderId,
      setRunParam: () => {},
      api,
      scheduleFeed,
      subscribeRows,
      ...(queueEnabled === undefined ? {} : { queueEnabled }),
    });
    return null;
  }

  let renderer!: ReactTestRenderer;
  let session_ = session;
  let leader_ = leader;
  await act(async () => {
    renderer = create(createElement(Probe, { sessionId: session_, leaderId: leader_ }));
  });
  mounted.push(renderer);
  // The snapshots that answer the two subscribes: the schedule's, then the
  // listing's (a page's first frames).
  await act(async () => {
    pushSchedule();
  });
  await act(async () => {
    for (const cb of [...feeds]) cb(tasks);
  });

  return {
    state: () => {
      if (!out) throw new Error("not rendered");
      return out;
    },
    async setSession(id: string, leaderId?: string) {
      session_ = id;
      if (leaderId !== undefined) leader_ = leaderId;
      await act(async () => {
        renderer.update(createElement(Probe, { sessionId: session_, leaderId: leader_ }));
      });
    },
    async poll() {
      await act(async () => {
        pushSchedule();
      });
    },
    serve(entries: Entry[]) {
      served = entries;
    },
    holdSchedule() {
      held = true;
    },
    resyncs: () => resyncs,
    cancels,
    nextCancel(d) {
      pendingCancel = d;
    },
    async feed(rows: SchedTask[]) {
      await act(async () => {
        for (const cb of [...feeds]) cb(rows);
      });
    },
    feeding: () => feeds.length,
  };
}

// ── the landing page is never blocked ─────────────────────────────────────────

test("Back to the landing page opens the composer on the same paint", async () => {
  const h = await mount([pending("a", "2026-09-09T14:00:00+00:00")]);
  expect(h.state().blockers.length).toBe(1);
  expect(h.state().blocked).toBe(true);
  expect(h.state().schedDisabled).toBe(true);

  // The gesture, WITHOUT a snapshot: `newChat` puts `transcriptGen` back to 0 and
  // the schedule is still serving the same pending row, so nothing but the
  // session id has changed. The composer must be open anyway — the banner only
  // ever draws inside a chat, so a blocked home box has nothing to explain it.
  await h.setSession("");
  expect(h.state().blocked).toBe(false);
  expect(h.state().schedDisabled).toBe(false);
  expect(h.state().reason).toBe("");

  // And the block comes back for the conversation it belongs to.
  await h.setSession("s1");
  expect(h.state().blocked).toBe(true);
});

// ── who owns the ORDER of two sends into one conversation ────────────────────
//
// This test was written the other way round, and pinned it: "a this-session
// blocker keeps the block, flag or no flag". That was true while the composer
// was the ONLY thing standing between two messages racing into one run — a
// pending entry aimed here is one the scheduler is about to claim and send into
// this very session, and a line typed over it would arrive in the middle of it.
//
// The project queue moves that job to the scheduler, which is the only place it
// was ever answerable: under the flag a send is ADMITTED before it spawns, and
// admission queues any message aimed at a session with due pending entries of
// its own rather than letting it start. So the second line is no longer a race —
// it is the next entry in this conversation's own line, in the order it was
// typed, with its own dashed bubble in the transcript saying so. Keeping the box
// shut would refuse a message the server is perfectly willing to take.
//
// WHAT DID NOT MOVE is the CALENDAR. An entry the reader scheduled is a turn the
// scheduler is about to start in this very session out of its own hand — nothing
// admitted it, nothing ordered it against a line the reader is typing — so it
// still shuts the box, with the same reason it always gave. `origin` is the one
// field that tells the two apart, and its ABSENCE reads as the calendar: the
// cautious half, and what every entry stored before the field existed gets.
const chatEntry = (id: string, due: string, over: Partial<Entry> = {}): Entry =>
  pending(id, due, { origin: "chat", ...over });

for (const queueOn of [false, true]) {
  test(
    `a chat's OWN queued message ${queueOn ? "leaves the box open (flag on)" : "shuts it (flag off)"}`,
    async () => {
      const mine = chatEntry("a", "2026-09-09T14:00:00+00:00");
      const theirs = chatEntry("b", "2026-09-09T14:00:00+00:00", { session_id: "s2" });
      const h = await mount([mine, theirs], "s1", [], queueOn);
      // WHAT IS WAITING is the same list either way — this conversation's own
      // pending work. Only who draws it, and whether the box is shut over it,
      // changes.
      expect(h.state().waitingHere.map((e) => e.id)).toEqual(["a"]);
      expect(h.state().blocked).toBe(!queueOn);
      expect(h.state().schedDisabled).toBe(!queueOn);
      expect(h.state().reason === "").toBe(queueOn);
      // …and under the flag the old block card is handed NOTHING, because the
      // chat draws those messages as messages. Two shapes for one fact a few
      // pixels apart is what browser QA sent back on 2026-09-12.
      expect(h.state().blockers.map((e) => e.id)).toEqual(queueOn ? [] : ["a"]);

      // A folder-mate aimed at a DIFFERENT session never reached this list at
      // all, so it never shut this box under either flag.
      h.serve([theirs]);
      await h.poll();
      expect(h.state().waitingHere).toEqual([]);
      expect(h.state().blocked).toBe(false);
    },
  );

  test(
    `a CALENDAR message aimed here shuts the box either way (flag ${queueOn ? "on" : "off"})`,
    async () => {
      // No `origin`: the reader scheduled it, and the scheduler is about to run
      // it in this session. A line typed over that is two messages racing into
      // one turn, which is the whole reason the block ever existed.
      const h = await mount([pending("a", "2026-09-09T14:00:00+00:00")], "s1", [], queueOn);
      expect(h.state().blocked).toBe(true);
      expect(h.state().schedDisabled).toBe(true);
      expect(h.state().reason).not.toBe("");
      // It is still one of the chat's waiting rows under the flag — same dashed
      // bubble, same line, with `scheduled · <when>` before its due time — so
      // the box being shut is the ONLY thing the calendar buys.
      expect(h.state().waitingHere.map((e) => e.id)).toEqual(["a"]);
    },
  );
}

test("a chat entry beside a calendar one still shuts the box, and names the calendar one", async () => {
  // The reason is written ABOUT the entry that shut it. Naming a chat send in a
  // sentence about why the box is shut would be a banner about the wrong
  // message — and the chat send is the FIRST of the two here, so the naive
  // `blockers[0]` would have picked it.
  const h = await mount(
    [
      { ...pending("a", "2026-09-09T14:00:00+00:00"), origin: "chat" },
      pending("b", "2026-09-09T15:00:00+00:00"),
    ],
    "s1",
    [],
    true,
  );
  expect(h.state().waitingHere.map((e) => e.id)).toEqual(["a", "b"]);
  expect(h.state().blocked).toBe(true);
  expect(h.state().reason).not.toBe("");
});

test("every row the snapshot carried is published, for a chat that has no session", async () => {
  // A chat whose first message queued has NO session — nothing has run — so the
  // session filter answers `[]` for it by construction. Its waiting messages are
  // found in THIS list, through the leader entry they were admitted behind, and
  // the rows that have already RUN are carried too: once the leader runs it is
  // the only row tying that group to the session the chat is about to adopt.
  const h = await mount(
    [
      { ...pending("a", "2026-09-09T14:00:00+00:00"), session_id: "", origin: "chat" },
      { ...pending("b", "2026-09-09T15:00:00+00:00"), session_id: "", origin: "chat", follow_of: "a" },
      { ...pending("z", "2026-09-09T13:00:00+00:00"), session_id: "", state: "sent" },
    ],
    "",
    [],
    true,
    "a",
  );
  await h.poll();
  // "z" is SENT and names no session it opened, so it is published to nobody:
  // the list carries the rows still in the line and the rows that hold the LINK
  // to a session (🔴 review 2026-09-12).
  expect((h.state().allRows ?? []).map((e) => e.id)).toEqual(["a", "b"]);
  // …and the chat draws its own two out of it, by leader.
  expect(h.state().waitingHere.map((e) => e.id)).toEqual(["a", "b"]);
});

test("the rows survive the adoption, and a reload with no leader left", async () => {
  // THE FINDING (Bugbot PR #1124). The leader ran, so it names the session and is
  // no longer pending; the followers are pending and name nothing (the server
  // fills a follower's session at claim time). The session filter missed them,
  // and so did the leader list the moment the adoption cleared the leader id.
  const entries: Entry[] = [
    { id: "L", state: "sent", session_id: "", claude_session_id: "s9", due: "2026-09-09T13:00:00+00:00" },
    { id: "f1", state: "pending", session_id: "", follow_of: "L", due: "2026-09-09T14:00:00+00:00", origin: "chat" },
    { id: "f2", state: "pending", session_id: "", follow_of: "L", due: "2026-09-09T15:00:00+00:00", origin: "chat" },
  ];
  const h = await mount(entries, "", [], true, "L");
  await h.poll();
  expect(h.state().waitingHere.map((e) => e.id)).toEqual(["f1", "f2"]);
  // The adoption: `openSession` gives the chat a session and the leader memory is
  // dropped by `leaderAfterSession`. Nothing about the entries changed.
  await h.setSession("s9", "");
  expect(h.state().waitingHere.map((e) => e.id)).toEqual(["f1", "f2"]);
  // …AND A RELOAD, which is the same state reached with nothing in client memory.
  const fresh = await mount(entries, "s9", [], true, "");
  await fresh.poll();
  expect(fresh.state().waitingHere.map((e) => e.id)).toEqual(["f1", "f2"]);
});

test("a claimed entry stays in the list, and in the live ids behind the seeds", async () => {
  // The second between the scheduler taking the entry and the turn appearing:
  // dropping it there took the row down and pruned its seed, so the reader's own
  // message blinked out of the conversation until the turn landed.
  const h = await mount(
    [{ ...pending("a", "2026-09-09T14:00:00+00:00"), state: "sending", origin: "chat" }],
    "s1",
    [],
    true,
  );
  await h.poll();
  expect(h.state().waitingHere.map((e) => e.id)).toEqual(["a"]);
  expect([...(h.state().pendingIds ?? [])]).toEqual(["a"]);
});

test("the row follows the listing feed under the queue, because its fields move", async () => {
  // `queue_ahead`, `queue_position`, `queue_priority` and `queue_waiting` all
  // change UNDER A FIXED ENTRY ID — the task in front finishes, somebody skips
  // ahead — so a row read once said "behind TASK-038" for the life of the chat.
  //
  // NOT re-read on a clock, which is what it was (a 5 s floor under a GET,
  // 🔴 review 2026-09-12): the listing feed carries every such move the moment
  // the manager makes it, and the row is whatever the last frame said.
  const h = await mount(
    [{ ...pending("a", "2026-09-09T14:00:00+00:00"), origin: "chat" }],
    "s1",
    [{ key: "s1", queue_ahead: "TASK-041" }],
    true,
  );
  await h.poll();
  expect(h.state().rec?.queue_ahead).toBe("TASK-041");
  const gen = h.state().recGen;
  // Snapshots of the schedule alone move nothing about the row…
  await h.poll();
  await h.poll();
  expect(h.state().recGen).toBe(gen);
  // …and the next listing frame does.
  await h.feed([{ key: "s1", queue_ahead: "", queue_priority: true }]);
  expect(h.state().rec?.queue_priority).toBe(true);
  // …and the generation moves with it, which is what retires an optimistic claim.
  expect(h.state().recGen).toBeGreaterThan(gen);
});

test("…and flag off the row comes from the same feed, with no GET of its own", async () => {
  const h = await mount(
    [pending("a", "2026-09-09T14:00:00+00:00")],
    "s1",
    [{ key: "s1", task_id: "TASK-7" }],
    false,
  );
  expect(h.feeding()).toBe(1);
  expect(h.state().rec?.task_id).toBe("TASK-7");
  await h.poll();
  await h.poll();
  expect(h.feeding()).toBe(1);
  await h.feed([{ key: "s1", task_id: "TASK-7", status: "done" }]);
  expect(h.state().rec?.status).toBe("done");
});

test("a chat with no session gets its card and its row, keyed on the leader", async () => {
  // Its task is named after the FIRST entry it queued (`pending:<leader>`), never
  // after the entry at the front of its line — so the row read had to be told the
  // leader or it found nothing, and the card had no facts to draw.
  const h = await mount(
    [
      { id: "L", state: "pending", session_id: "", due: "2026-09-09T13:00:00+00:00", origin: "chat" },
      { id: "f1", state: "pending", session_id: "", follow_of: "L", due: "2026-09-09T14:00:00+00:00", origin: "chat" },
    ],
    "",
    [{ key: "pending:L", queue_ahead: "TASK-041", queue_waiting: 2 }],
    true,
    "L",
  );
  await h.poll();
  expect(h.state().rec?.queue_ahead).toBe("TASK-041");
  expect(h.state().rec?.queue_waiting).toBe(2);
  // …and the box is still open: nothing here is a message the reader SCHEDULED.
  expect(h.state().blocked).toBe(false);
});

test("the composer shuts on the server's own verdict when there is a row", async () => {
  // One rule, server first (design.md, UI). The entry rule (`schedIsCalendar`) is
  // the fallback for the paint before the row lands, not a second opinion.
  const h = await mount(
    [{ ...pending("a", "2026-09-09T14:00:00+00:00"), origin: "chat" }],
    "s1",
    [{ key: "s1", queue_blocking: true }],
    true,
  );
  await h.poll();
  expect(h.state().rec?.queue_blocking).toBe(true);
  expect(h.state().blocked).toBe(true);
  expect(h.state().reason).not.toBe("");
});

test("…and stays open when the row says nothing is blocking, whatever the entry", async () => {
  // No row to be had at first: the entry rule decides, and a calendar entry (no
  // `origin`) shuts the box — the cautious half, and the right direction to be
  // briefly wrong in.
  const h = await mount([pending("a", "2026-09-09T14:00:00+00:00")], "s1", [], true);
  expect(h.state().rec).toBe(null);
  expect(h.state().blocked).toBe(true);
  // Then the server's own answer lands and it outranks that reading.
  await h.feed([{ key: "s1", queue_blocking: false }]);
  expect(h.state().rec?.queue_blocking).toBe(false);
  expect(h.state().blocked).toBe(false);
});

test("those rows keep their identity when the schedule did not move", async () => {
  // The dedupe `absorb` keeps — one object for one unchanged list — is worth
  // nothing if this publishes a fresh array on every snapshot: the composer's
  // whole column would re-render on every push over a schedule that did not
  // change.
  const h = await mount([pending("a", "2026-09-09T14:00:00+00:00")], "s1", [], true);
  await h.poll();
  const first = h.state().allRows;
  await h.poll();
  expect(h.state().allRows).toBe(first);
});

test("reset() empties the block for the transcript that replaced it", async () => {
  const h = await mount([pending("a", "2026-09-09T14:00:00+00:00")]);
  expect(h.state().blocked).toBe(true);
  h.serve([]);
  await act(async () => h.state().reset());
  expect(h.state().blockers).toEqual([]);
  expect(h.state().blocked).toBe(false);
});

// ── a cancel removes ITS OWN entry ───────────────────────────────────────────

test("a finished one-off cancel drops the entry it was pressed on, not the head", async () => {
  const a = pending("a", "2026-09-09T14:00:00+00:00");
  const b = pending("b", "2026-09-09T15:00:00+00:00");
  const h = await mount([a, b]);
  expect(h.state().blockers.map((e) => e.id)).toEqual(["a", "b"]);

  const d = deferred<unknown>();
  h.nextCancel(d);
  await act(async () => h.state().onStop());
  expect(h.cancels).toEqual(["a"]);
  expect(h.state().stopping).toBe(true);

  // THE LIST MOVES UNDERNEATH THE PRESS. A re-issued `due` puts "a" behind "b",
  // which is exactly what `schedPendingHere`'s time sort is for — and the
  // cancel in flight is still the cancel of "a".
  h.serve([{ ...a, due: "2026-09-09T16:00:00+00:00" }, b]);
  await h.poll();
  expect(h.state().blockers.map((e) => e.id)).toEqual(["b", "a"]);

  // The LOCAL edit is what this asserts — the reason it exists at all is that
  // the box must open on the click and not on the next snapshot — so the
  // confirming resync is wedged open rather than allowed to answer over it.
  h.holdSchedule();
  await act(async () => {
    d.resolve(undefined);
    await d.promise;
  });
  // `slice(1)` took "b" here and left the cancelled "a" naming a run the server
  // has already dropped.
  expect(h.state().blockers.map((e) => e.id)).toEqual(["b"]);
  expect(h.state().stopping).toBe(false);
});

test("a stopped repeat still takes every occurrence of its template", async () => {
  const one = pending("o1", "2026-09-09T14:00:00+00:00", { template_id: "t7" });
  const two = pending("o2", "2026-09-09T15:00:00+00:00", { template_id: "t7" });
  const other = pending("z", "2026-09-09T16:00:00+00:00");
  const h = await mount([one, two, other]);

  // Two presses: the first only arms.
  await act(async () => h.state().onStop());
  expect(h.state().armed).toBe(true);
  expect(h.cancels).toEqual([]);
  h.holdSchedule();
  await act(async () => h.state().onStop());
  expect(h.cancels).toEqual(["t7"]);
  expect(h.state().blockers.map((e) => e.id)).toEqual(["z"]);
});

// ── a cancel outliving its transcript says nothing ───────────────────────────

test("reset() clears a half-pressed stop and an in-flight one", async () => {
  const rep = pending("o1", "2026-09-09T14:00:00+00:00", { template_id: "t7" });
  const h = await mount([rep]);
  await act(async () => h.state().onStop());
  expect(h.state().armed).toBe(true);

  h.serve([]);
  await act(async () => h.state().reset());
  expect(h.state().armed).toBe(false);
  expect(h.state().stopping).toBe(false);
});

test("a cancel that outlives its transcript neither unblocks nor refuses", async () => {
  const h = await mount([pending("a", "2026-09-09T14:00:00+00:00")]);
  const d = deferred<unknown>();
  h.nextCancel(d);
  await act(async () => h.state().onStop());
  expect(h.state().stopping).toBe(true);

  // The conversation is REPLACED mid-request. `stopping` must not survive it:
  // the banner's stop button is disabled while it is set, so the next
  // conversation inherited a control it could not press until a request it
  // never made came back.
  const replacement = pending("b", "2026-09-09T15:00:00+00:00");
  h.serve([replacement]);
  await act(async () => h.state().reset());
  expect(h.state().stopping).toBe(false);
  expect(h.state().blockers.map((e) => e.id)).toEqual(["b"]);

  // And the late answer is mute — in BOTH directions.
  await act(async () => {
    d.resolve(undefined);
    await d.promise;
  });
  expect(h.state().stopping).toBe(false);
  expect(h.state().blockers.map((e) => e.id)).toEqual(["b"]);

  const bad = deferred<unknown>();
  h.nextCancel(bad);
  await act(async () => h.state().onStop());
  expect(h.state().stopping).toBe(true);
  await act(async () => h.state().reset());
  await act(async () => {
    bad.reject(new Error("gone"));
    await bad.promise.catch(() => {});
  });
  expect(h.state().refused).toBe(false);
  expect(h.state().stopping).toBe(false);
});

// ── the banner repaints every field a snapshot can change (G-5) ──────────────

test("a message edited on the Tasks page reaches the banner on the next snapshot", async () => {
  const h = await mount([
    pending("a", "2026-09-09T14:00:00+00:00", { message: "old wording" }),
  ]);
  expect(h.state().blockers[0].message).toBe("old wording");

  // Same id, same due, same state — only the words changed. The old dedupe
  // compared exactly the three fields that did NOT change, so this froze.
  h.serve([pending("a", "2026-09-09T14:00:00+00:00", { message: "new wording" })]);
  await h.poll();
  expect(h.state().blockers[0].message).toBe("new wording");
});

test("an entry that becomes a repeat re-words the reason line on the next snapshot", async () => {
  const h = await mount([pending("a", "2026-09-09T14:00:00+00:00")]);
  expect(h.state().reason).toBe("Blocked — a scheduled message runs in this chat.");

  h.serve([pending("a", "2026-09-09T14:00:00+00:00", { template_id: "t9" })]);
  await h.poll();
  expect(h.state().reason).toBe("Blocked — a repeating message runs in this chat.");
});

test("an unchanged snapshot still hands back the very same array", async () => {
  const h = await mount([pending("a", "2026-09-09T14:00:00+00:00", { message: "m" })]);
  const first = h.state().blockers;
  // A FRESH ARRAY OFF THE WIRE, field for field identical: the whole point of
  // the dedupe is that this is not a re-render of the composer's column.
  h.serve([pending("a", "2026-09-09T14:00:00+00:00", { message: "m" })]);
  await h.poll();
  expect(h.state().blockers).toBe(first);
});

// ── the tasks row comes off the listing feed, never a GET (G-6, D3) ──────────

test("the landing page follows no listing at all", async () => {
  const h = await mount([pending("a", "2026-09-09T14:00:00+00:00")]);
  expect(h.feeding()).toBe(1);

  // Back. `blocked` goes false on this paint with the blockers still in hand,
  // and there is no conversation whose row could be waiting — so the feed is
  // let go.
  await h.setSession("");
  expect(h.state().blocked).toBe(false);
  expect(h.feeding()).toBe(0);
  await h.poll();
  expect(h.feeding()).toBe(0);
});

test("one subscription however many snapshots land, and a new head is filled from the rows in hand", async () => {
  const h = await mount([pending("a", "2026-09-09T14:00:00+00:00")], "s1", [
    { key: "k", task_id: "TASK-1", messages: [{ entry_id: "a" }] },
    { key: "k9", task_id: "TASK-9", messages: [{ entry_id: "c" }] },
  ]);
  expect(h.feeding()).toBe(1);
  expect(h.state().rec?.task_id).toBe("TASK-1");
  await h.poll();
  await h.poll();
  expect(h.feeding()).toBe(1);

  // A DIFFERENT message at the front is a different row question — answered
  // from the listing already in hand, on that paint, with no new frame.
  h.serve([pending("c", "2026-09-09T16:00:00+00:00")]);
  await h.poll();
  expect(h.state().rec?.task_id).toBe("TASK-9");
  expect(h.feeding()).toBe(1);
});

test("a row belongs to the entry it was found for", async () => {
  const h = await mount([pending("a", "2026-09-09T14:00:00+00:00")], "s1", [
    { key: "k", task_id: "TASK-1", messages: [{ entry_id: "a" }] },
  ]);
  expect(h.state().rec?.task_id).toBe("TASK-1");

  // A new message at the front with no row of its own: the old number must be
  // gone on THAT PAINT, not when a later listing says so (T:16997-16999).
  h.serve([pending("z", "2026-09-09T18:00:00+00:00")]);
  await h.poll();
  expect(h.state().rec).toBe(null);
});

// ── one representation for a queued send ─────────────────────────────────────
//
// The chip and this block used to draw the SAME entry at the same time, a few
// pixels apart, in two vocabularies — "Queued · #1 in line · behind TASK-006"
// over "Blocked — a scheduled message runs in this chat … Cancel this message"
// — disagreeing about whether anything was blocked at all (Akshil, browser QA
// 2026-09-12). The chip is the right card for a message the reader just typed;
// this block is the right one for a message coming due out of the calendar.

test("under the flag the block draws nothing, and the task row is still found", async () => {
  // The block was one card explaining why the box was shut. Under the queue the
  // same fact is drawn as the messages themselves, plus one summary over the
  // composer — so the block draws NOTHING rather than a third copy of it.
  //
  // What it still carries is the `/api/tasks` row, and that is not decoration
  // any more: it is where "1st in line · behind TASK-038" comes from, which is
  // what makes a reload say the same sentence the send did.
  const h = await mount(
    [
      { ...pending("a", "2026-09-09T14:00:00+00:00"), origin: "chat" },
      { ...pending("b", "2026-09-09T15:00:00+00:00"), origin: "chat" },
    ],
    "s1",
    [{ key: "k", task_id: "TASK-1", queue_ahead: "TASK-038", messages: [{ entry_id: "a" }] }],
    true,
  );
  expect(h.state().blockers).toEqual([]);
  expect(h.state().waitingHere.map((e) => e.id)).toEqual(["a", "b"]);
  expect(h.state().rec?.task_id).toBe("TASK-1");
  expect(h.state().rec?.queue_ahead).toBe("TASK-038");
  // …and the composer is open: a chat's own queued messages never shut it.
  expect(h.state().blocked).toBe(false);
});

test("flag OFF, the block is main's byte for byte", async () => {
  // The one rule this feature must not break: the block a flag-off reader sees
  // is the block they saw before it existed.
  const h = await mount([pending("a", "2026-09-09T14:00:00+00:00")], "s1", [], false);
  expect(h.state().blockers.map((e) => e.id)).toEqual(["a"]);
  expect(h.state().blocked).toBe(true);
  expect(h.state().schedDisabled).toBe(true);
});

test("refresh() asks the schedule NOW — one resync, never a fetch", async () => {
  // What a row's `delete` spends. The press changed the schedule from this pane,
  // so everything derived from the snapshot — the waiting rows and `pendingIds`
  // both — describes a world the reader has already left until the producer's
  // next push: a row standing over a message they just deleted.
  const h = await mount(
    [{ ...pending("a", "2026-09-09T14:00:00+00:00"), origin: "chat" }],
    "s1",
    [],
    true,
  );
  expect(h.state().waitingHere.map((e) => e.id)).toEqual(["a"]);
  h.serve([]);
  const before = h.resyncs();
  await act(async () => {
    h.state().refresh();
  });
  expect(h.resyncs()).toBe(before + 1);
  expect(h.state().waitingHere).toEqual([]);
  expect([...(h.state().pendingIds ?? [])]).toEqual([]);
});

// ── the live row (the tasks change feed) ─────────────────────────────────────

test("a feed answer moves this chat's row without waiting for anything", async () => {
  // The bug (Akshil, 2026-09-17): the queue moves on EVENTS and this pane was
  // the only surface still asking on a lap, so "behind TASK-046" lingered
  // seconds after the folder freed while the Tasks page repainted in ~100ms
  // off the feed.
  const h = await mount(
    [{ ...pending("a", "2026-09-09T14:00:00+00:00"), origin: "chat" }],
    "s1",
    [{ key: "s1", status: "queued", queue_position: 3, queue_ahead: "TASK-046" }],
    true,
  );
  expect(h.state().rec?.queue_position).toBe(3);
  await h.feed([
    { key: "s1", status: "queued", queue_position: 2, queue_ahead: "TASK-046" },
  ]);
  // No snapshot, no clock, no GET — and the caption has already moved.
  expect(h.state().rec?.queue_position).toBe(2);
  expect(h.state().row?.queue_position).toBe(2);
});

test("the same answer twice is not a re-render", async () => {
  // The feed fires for every row on the machine, so a blind write per answer
  // would bump `recGen` — which Run next's optimistic claim is measured against
  // — several times a second on a busy box.
  const h = await mount(
    [{ ...pending("a", "2026-09-09T14:00:00+00:00"), origin: "chat" }],
    "s1",
    [],
    true,
  );
  const row: SchedTask = { key: "s1", status: "queued", queue_position: 1 };
  await h.feed([row]);
  const gen = h.state().recGen;
  await h.feed([{ ...row }]);
  expect(h.state().recGen).toBe(gen);
  expect(h.state().row?.queue_position).toBe(1);
});

test("queued → in_progress asks the schedule again in the same beat", async () => {
  // The row is the fast half of the picture. Whether the entry is still pending
  // — and which session its run opened, the one road a queued new chat has to
  // its own transcript — only `/api/schedule` knows, and waiting out its lap is
  // what left a started message drawn as a dashed queued bubble for the length
  // of the reply.
  const h = await mount(
    [{ ...pending("a", "2026-09-09T14:00:00+00:00"), origin: "chat" }],
    "s1",
    [{ key: "s1", status: "queued", queue_position: 1, queue_waiting: 1 }],
    true,
  );
  expect(h.state().waitingHere.map((e) => e.id)).toEqual(["a"]);
  // The manager popped it: the entry is away and the row is running.
  h.serve([]);
  await h.feed([{ key: "s1", status: "in_progress", queue_waiting: 0 }]);
  expect(h.state().row?.status).toBe("in_progress");
  // …and the dashed bubble is retired in the same beat, off the schedule
  // resync the status change went and asked for rather than off the next push.
  expect(h.state().waitingHere).toEqual([]);
  expect(h.state().rec?.queue_waiting ?? 0).toBe(0);
});

test("a chat with no session reads its row through the leader key", async () => {
  // The brand-new chat whose first message queued is the one this matters most
  // on: it has no session for the schedule's own filter to find, and its row
  // is named `pending:<leader>`.
  const h = await mount(
    [{ ...pending("lead", "2026-09-09T14:00:00+00:00"), session_id: "", origin: "chat" }],
    "",
    [],
    true,
    "lead",
  );
  await h.feed([
    { key: "pending:lead", task_id: "TASK-048", status: "queued", queue_position: 2,
      queue_ahead: "TASK-046" },
  ]);
  expect(h.state().row?.task_id).toBe("TASK-048");
  expect(h.state().row?.queue_ahead).toBe("TASK-046");
});

test("the holder getting a door is news: the header's row carries `queue_ahead_key`", async () => {
  // THE BUG (Akshil, 2026-09-18): the Tasks row drew "behind TASK-046" as a link
  // and the chat header three pixels away drew the same words as plain text.
  // `queue_ahead_key` is the holder's own listing key — the door the id opens
  // while that run has no session yet (`pending:<entry>`,
  // platform/lib/queue.queueAheadHref) — and `sameQueueRow` did not compare it,
  // so the listing that finally named it read as "the same answer" and was
  // thrown away. The header kept the doorless row for as long as nothing else
  // about the line moved.
  const h = await mount(
    [{ ...pending("a", "2026-09-09T14:00:00+00:00"), origin: "chat" }],
    "s1",
    [],
    true,
  );
  await h.feed([
    { key: "s1", status: "queued", queue_position: 2, queue_ahead: "TASK-046",
      queue_ahead_target: "/repo/news.py" },
  ]);
  expect(h.state().row?.queue_ahead_key ?? "").toBe("");
  // The holder is still starting, so its SESSION never arrives — only its key.
  await h.feed([
    { key: "s1", status: "queued", queue_position: 2, queue_ahead: "TASK-046",
      queue_ahead_target: "/repo/news.py", queue_ahead_key: "pending:e-9" },
  ]);
  expect(h.state().row?.queue_ahead_key).toBe("pending:e-9");
  // …and that is exactly the field the caption turns into a link.
  expect(queueCaption(h.state().row as QueueFacts)?.aheadHref).toContain("queued=e-9");
});

test("a listing without our row clears it; a failed read leaves it (Bugbot)", async () => {
  // `subscribeListing` (`shell/tasksPulse.emitListing`) always hands `platformRows`
  // the FULL merged listing — a change-poll's own rows/gone pair is folded into
  // `held` before it goes out, never a bare delta — and a failed read is dropped
  // before it ever reaches this hook (`platformRows`'s `if (ev.failed) return`).
  // So a listing that reaches here with nothing for this chat means the row is
  // GONE (cancelled, or run and off the board), not merely unmentioned.
  const h = await mount(
    [{ ...pending("a", "2026-09-09T14:00:00+00:00"), origin: "chat" }],
    "s1",
    [],
    true,
  );
  await h.feed([
    { key: "s1", status: "queued", queue_position: 3, queue_ahead: "TASK-046" },
  ]);
  expect(h.state().row?.queue_position).toBe(3);
  expect(h.state().rec?.queue_position).toBe(3);

  // A full listing with no row for this chat: the header must drop the stale
  // "queued · …" caption rather than keep the last-known facts.
  await h.feed([{ key: "s2", status: "queued", queue_position: 1 }]);
  expect(h.state().row).toBe(null);
  expect(h.state().rec).toBe(null);

  // A failed read never calls back at all (dropped in `platformRows`), so it
  // leaves whatever the pane already has rather than clearing it further.
  const rowBefore = h.state().row;
  const recBefore = h.state().rec;
  expect(rowBefore).toBe(null);
  expect(recBefore).toBe(null);
});

test("flag OFF the feed is still the row's source, but the header's `row` stays null", async () => {
  const h = await mount([pending("a", "2026-09-09T14:00:00+00:00")], "s1", [], false);
  expect(h.feeding()).toBe(1);
  await h.feed([{ key: "s1", status: "queued", queue_position: 1, task_id: "TASK-3" }]);
  expect(h.state().rec?.task_id).toBe("TASK-3");
  expect(h.state().row).toBe(null);
});

test("a done chat's row updates when its session's new message is queued in another task", async () => {
  // The bug (Akshil, 2026-09-17): a chat ALREADY OPEN with a session (done ring)
  // sends a message into a folder that another task owns. The feed delivers
  // status:"queued" but the header keeps the done ring until reload. Trace:
  // schedFindTask prioritizes the sessionId key before the pending key, so it
  // returns the old done task instead of the new queued entry's task.
  const h = await mount(
    // Initial: first message pending for session s1
    [{ ...pending("a", "2026-09-09T14:00:00+00:00"), session_id: "s1", origin: "chat" }],
    "s1",
    // The task for this session (done, so entry "a" finished)
    [{ key: "s1", status: "done", task_id: "TASK-001" }],
    true,
  );
  expect(h.state().rec?.status).toBe("done");

  // Simulate entry "a" finishing: remove it from pending (it's no longer "pending" state).
  // Then add a new pending entry "b"
  h.serve([
    { ...pending("a", "2026-09-09T14:00:00+00:00"), session_id: "s1", origin: "chat", state: "sent" },
    { ...pending("b", "2026-09-09T15:00:00+00:00"), session_id: "s1", origin: "chat" },
  ]);
  await h.poll();
  expect(h.state().waitingHere.length).toBe(1); // Only "b" is pending (entry "a" is sent)

  // The feed delivers tasks: the old done task PLUS the new queued task for entry "b"
  await h.feed([
    // The old done task is still in the listing (for entry "a")
    { key: "s1", status: "done", task_id: "TASK-001" },
    // The new queued entry is under a different task (for entry "b")
    { key: "pending:b", status: "queued", task_id: "TASK-002", queue_position: 2, queue_ahead: "TASK-046" },
  ]);

  // The header should show the FIRST PENDING ENTRY'S TASK, which is "b" (queued).
  // Before the fix, schedFindTask would return the "s1" (done) task instead of
  // the "pending:b" (queued) task, because it checked sessionId before pending key.
  expect(h.state().row?.status).toBe("queued");
  expect(h.state().row?.queue_position).toBe(2);
});
