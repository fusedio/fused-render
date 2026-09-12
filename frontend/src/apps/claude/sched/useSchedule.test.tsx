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
import type { ScheduleApi, ScheduleState } from "./useSchedule";
import type { SchedEntry, SchedTask } from "./scheduled";
import type { ChatController } from "../protocol/controller-api";

const { useSchedule } = await import("./useSchedule");

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
  /** Re-render with a different session on screen — no tick, no fetch. */
  setSession(id: string): Promise<void>;
  /** Run the poll's interval callback and flush it. */
  poll(): Promise<void>;
  /** What `/api/schedule` answers next. */
  serve(entries: Entry[]): void;
  /** Wedge `/api/schedule` open, so the confirming tick a cancel fires in its
   *  `finally` cannot answer over the local edit under test. */
  holdSchedule(): void;
  cancels: string[];
  /** The next cancel's answer, installed by the test. */
  nextCancel(d: { promise: Promise<unknown> }): void;
  /** How many full `/api/tasks` listings this mount has paid for. */
  tasksReads(): number;
  /** What `/api/tasks` answers next. */
  serveTasks(tasks: SchedTask[]): void;
  /** Wedge `/api/tasks` open, so the single-flight guard is observable. */
  holdTasks(): void;
  /** Let a wedged `/api/tasks` answer. */
  releaseTasks(): Promise<void>;
  /** The entry ids the chat is drawing a chip for — the block's filter. */
  setChips(ids: string[]): Promise<void>;
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
  /** Entries the chat is already drawing a Queued chip for: the block does not
   *  draw those (flag on only). */
  chips: string[] = [],
): Promise<Harness> {
  let served = initial;
  let servedTasks: SchedTask[] = tasks;
  let tasksReads = 0;
  let taskGate: { promise: Promise<void>; open(): void } | null = null;
  const cancels: string[] = [];
  let pendingCancel: { promise: Promise<unknown> } | null = null;
  let held: Promise<never> | null = null;
  const api: Api = {
    getSchedule: async () => {
      if (held) await held;
      return { entries: served };
    },
    getTasks: async () => {
      tasksReads += 1;
      if (taskGate) await taskGate.promise;
      return { tasks: servedTasks };
    },
    cancelScheduledMessage: async (id) => {
      cancels.push(id);
      if (!pendingCancel) return undefined;
      const d = pendingCancel;
      pendingCancel = null;
      return d.promise;
    },
  };
  const beats: Array<() => void> = [];
  const timers = {
    setInterval: (fn: () => void) => {
      beats.push(fn);
      return beats.length;
    },
    clearInterval: () => {},
  };

  let out: State | null = null;
  function Probe(props: { sessionId: string; chips: string[] }) {
    out = useSchedule({
      controller,
      file: "/w/app",
      sessionId: props.sessionId,
      inChat: !!props.sessionId,
      setRunParam: () => {},
      api,
      timers,
      chipEntryIds: props.chips,
      ...(queueEnabled === undefined ? {} : { queueEnabled }),
    });
    return null;
  }

  let renderer!: ReactTestRenderer;
  let session_ = session;
  let chips_ = chips;
  await act(async () => {
    renderer = create(createElement(Probe, { sessionId: session_, chips: chips_ }));
  });
  mounted.push(renderer);

  return {
    state: () => {
      if (!out) throw new Error("not rendered");
      return out;
    },
    async setSession(id: string) {
      session_ = id;
      await act(async () => {
        renderer.update(createElement(Probe, { sessionId: session_, chips: chips_ }));
      });
    },
    async setChips(ids: string[]) {
      chips_ = ids;
      await act(async () => {
        renderer.update(createElement(Probe, { sessionId: session_, chips: chips_ }));
      });
    },
    async poll() {
      await act(async () => {
        for (const beat of beats) beat();
      });
    },
    serve(entries: Entry[]) {
      served = entries;
    },
    holdSchedule() {
      held = new Promise<never>(() => {});
    },
    cancels,
    nextCancel(d) {
      pendingCancel = d;
    },
    tasksReads: () => tasksReads,
    serveTasks(tasks: SchedTask[]) {
      servedTasks = tasks;
    },
    holdTasks() {
      let open!: () => void;
      const promise = new Promise<void>((res) => {
        open = res;
      });
      taskGate = { promise, open };
    },
    async releaseTasks() {
      const gate = taskGate;
      taskGate = null;
      await act(async () => {
        gate?.open();
      });
    },
  };
}

// ── the landing page is never blocked ─────────────────────────────────────────

test("Back to the landing page opens the composer on the same paint", async () => {
  const h = await mount([pending("a", "2026-09-09T14:00:00+00:00")]);
  expect(h.state().blockers.length).toBe(1);
  expect(h.state().blocked).toBe(true);
  expect(h.state().schedDisabled).toBe(true);

  // The gesture, WITHOUT a poll: `newChat` puts `transcriptGen` back to 0 and
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
// its own (`behind_own`) rather than letting it start. So the second line is no
// longer a race — it is the next entry in this conversation's own line, in the
// order it was typed, with a chip under its bubble saying so. Keeping the box
// shut would refuse a message the server is perfectly willing to take.
//
// Hence: parametrised on the flag, because BOTH sentences are true — one about a
// build where nothing orders those two sends, one about a build where something
// does.
for (const queueOn of [false, true]) {
  test(
    `a message aimed HERE ${queueOn ? "queues behind itself (flag on)" : "shuts the box (flag off)"}`,
    async () => {
      const mine = pending("a", "2026-09-09T14:00:00+00:00");
      const theirs = pending("b", "2026-09-09T14:00:00+00:00", { session_id: "s2" });
      const h = await mount([mine, theirs], "s1", [], queueOn);
      // The BLOCKERS are the same list either way — this conversation's own
      // pending work, which is what the card above the composer draws. Only
      // whether the box is shut over it changes.
      expect(h.state().blockers.map((e) => e.id)).toEqual(["a"]);
      expect(h.state().blocked).toBe(!queueOn);
      expect(h.state().schedDisabled).toBe(!queueOn);
      expect(h.state().reason === "").toBe(queueOn);

      // And a folder-mate aimed at a DIFFERENT session never reached `blockers`
      // in the first place, so it never shut this box under either flag — the
      // half of "the composer stays open" that was always true.
      h.serve([theirs]);
      await h.poll();
      expect(h.state().blockers).toEqual([]);
      expect(h.state().blocked).toBe(false);
    },
  );
}

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
  // the box must open on the click and not on the next poll — so the confirming
  // tick is wedged open rather than allowed to answer over it.
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

// ── the banner repaints every field the poll can change (G-5) ────────────────

test("a message edited on the Tasks page reaches the banner on the next tick", async () => {
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

test("an entry that becomes a repeat re-words the reason line on the next tick", async () => {
  const h = await mount([pending("a", "2026-09-09T14:00:00+00:00")]);
  expect(h.state().reason).toBe("Blocked — a scheduled message runs in this chat.");

  h.serve([pending("a", "2026-09-09T14:00:00+00:00", { template_id: "t9" })]);
  await h.poll();
  expect(h.state().reason).toBe("Blocked — a repeating message runs in this chat.");
});

test("an unchanged poll still hands back the very same array", async () => {
  const h = await mount([pending("a", "2026-09-09T14:00:00+00:00", { message: "m" })]);
  const first = h.state().blockers;
  // A FRESH ARRAY OFF THE WIRE, field for field identical: the whole point of
  // the dedupe is that this is not a re-render of the composer's column.
  h.serve([pending("a", "2026-09-09T14:00:00+00:00", { message: "m" })]);
  await h.poll();
  expect(h.state().blockers).toBe(first);
});

// ── the tasks row is read for the BLOCK, once (G-6) ──────────────────────────

test("the landing page pays for no tasks listing at all", async () => {
  const h = await mount([pending("a", "2026-09-09T14:00:00+00:00")]);
  expect(h.tasksReads()).toBe(1);

  // Back. `blocked` goes false on this paint with the blockers still in hand —
  // T reads the listing only `if (blocked)`, so this gesture is free.
  await h.setSession("");
  expect(h.state().blocked).toBe(false);
  expect(h.tasksReads()).toBe(1);
  await h.poll();
  expect(h.tasksReads()).toBe(1);
});

test("one listing per blocking message, however often the poll ticks", async () => {
  const h = await mount([pending("a", "2026-09-09T14:00:00+00:00")]);
  expect(h.tasksReads()).toBe(1);
  await h.poll();
  await h.poll();
  expect(h.tasksReads()).toBe(1);

  // A DIFFERENT message at the front is a different row, so it is read.
  h.serve([pending("b", "2026-09-09T15:00:00+00:00")]);
  await h.poll();
  expect(h.tasksReads()).toBe(2);
});

test("two rapid heads issue one listing, and the head that lost is filled after", async () => {
  const h = await mount([pending("a", "2026-09-09T14:00:00+00:00")]);
  // Wedge the NEXT read open, then move the head twice underneath it.
  h.holdTasks();
  h.serve([pending("b", "2026-09-09T15:00:00+00:00")]);
  await h.poll();
  h.serve([pending("c", "2026-09-09T16:00:00+00:00")]);
  await h.poll();
  // "a"'s read landed before the gate went up; "b" and "c" then shared ONE
  // wedged read rather than issuing two overlapping listings.
  expect(h.tasksReads()).toBe(2);

  h.serveTasks([{ key: "k", task_id: "TASK-9", messages: [{ entry_id: "c" }] }]);
  await h.releaseTasks();
  // The wedged answer belonged to "b", which is no longer the head, so it is
  // never published against "c" — the guard re-arms instead and reads again for
  // the id that actually is blocking.
  expect(h.tasksReads()).toBe(3);
  expect(h.state().rec?.task_id).toBe("TASK-9");
});

test("a row belongs to the entry it was read for", async () => {
  const h = await mount([pending("a", "2026-09-09T14:00:00+00:00")], "s1", [
    { key: "k", task_id: "TASK-1", messages: [{ entry_id: "a" }] },
  ]);
  expect(h.state().rec?.task_id).toBe("TASK-1");

  // A new message at the front: the old number must be gone on THAT PAINT, not
  // when the replacement listing lands (T:16997-16999).
  h.holdTasks();
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

test("the block does not draw an entry the chat is already chipping", async () => {
  const h = await mount(
    [
      pending("a", "2026-09-09T14:00:00+00:00"),
      pending("b", "2026-09-09T15:00:00+00:00"),
    ],
    "s1",
    [{ key: "k", task_id: "TASK-1", messages: [{ entry_id: "b" }] }],
    true,
    ["a"],
  );
  // "a" has a chip, so the block takes what is left — and takes it from the
  // FRONT, which is why the filter lives in the hook: the row's number is read
  // for `blockers[0]`, and a view that hid the first entry itself would label
  // the second one with the first one's task.
  expect(h.state().blockers.map((e) => e.id)).toEqual(["b"]);
  expect(h.state().rec?.task_id).toBe("TASK-1");

  // Every entry chipped: nothing left to draw, and `SchedBlock` renders null on
  // an empty list (it has always done exactly that).
  await h.setChips(["a", "b"]);
  expect(h.state().blockers).toEqual([]);
  // …and the composer is still open: the queue never shuts it.
  expect(h.state().blocked).toBe(false);

  // The chip comes down (its entry fired, or was cancelled from the Tasks page)
  // and the block has the message back.
  await h.setChips([]);
  expect(h.state().blockers.map((e) => e.id)).toEqual(["a", "b"]);
});

test("flag OFF, a chip id filters nothing — the block is main's byte for byte", async () => {
  // There are no chips with the flag off (nothing is ever admitted), so this is
  // belt and braces on the one rule this feature must not break: the block a
  // flag-off reader sees is the block they saw before it existed.
  const h = await mount(
    [pending("a", "2026-09-09T14:00:00+00:00")],
    "s1",
    [],
    false,
    ["a"],
  );
  expect(h.state().blockers.map((e) => e.id)).toEqual(["a"]);
  expect(h.state().blocked).toBe(true);
  expect(h.state().schedDisabled).toBe(true);
});

test("the filtered list keeps its identity when it removes nothing", async () => {
  // The dedupe `absorb` keeps — one object for one unchanged list — is worth
  // nothing if the filter hands back a fresh array on every render: the card
  // would re-render four times a minute over a list that did not move.
  const h = await mount(
    [pending("a", "2026-09-09T14:00:00+00:00")],
    "s1",
    [],
    true,
    ["zzz"],
  );
  const first = h.state().blockers;
  await h.setChips(["yyy"]);
  expect(h.state().blockers).toBe(first);
});
