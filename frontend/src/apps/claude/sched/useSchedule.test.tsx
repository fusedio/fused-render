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
import type { SchedEntry } from "./scheduled";
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
}

async function mount(initial: Entry[], session = "s1"): Promise<Harness> {
  let served = initial;
  const cancels: string[] = [];
  let pendingCancel: { promise: Promise<unknown> } | null = null;
  let held: Promise<never> | null = null;
  const api: Api = {
    getSchedule: async () => {
      if (held) await held;
      return { entries: served };
    },
    getTasks: async () => ({ tasks: [] }),
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
  function Probe(props: { sessionId: string }) {
    out = useSchedule({
      controller,
      file: "/w/app",
      sessionId: props.sessionId,
      inChat: !!props.sessionId,
      setRunParam: () => {},
      api,
      timers,
    });
    return null;
  }

  let renderer!: ReactTestRenderer;
  await act(async () => {
    renderer = create(createElement(Probe, { sessionId: session }));
  });
  mounted.push(renderer);

  return {
    state: () => {
      if (!out) throw new Error("not rendered");
      return out;
    },
    async setSession(id: string) {
      await act(async () => {
        renderer.update(createElement(Probe, { sessionId: id }));
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
