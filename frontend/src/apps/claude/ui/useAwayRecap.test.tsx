// THE GATES, which are the whole of this feature's cost control.
//
// A recap is a MODEL CALL — ~12s on the server and real money — fired by two
// events (`visibilitychange`, window `blur`/`focus`) that a browser emits many
// times an hour for reasons that have nothing to do with the reader leaving.
// Every one of the checks below is there because the obvious version of this
// hook would have paid for a call nobody wanted: a devtools click, a half-typed
// message the reader came back to finish, a fold they had already dismissed.
//
// The `window`/`document` seams are injected, `useDismissOnWindow.test.ts`
// style, rather than stubbing globals: the DOM shim's listeners are no-ops and
// shared by every suite in the process.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, expect, test } from "bun:test";
import { createElement } from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";

import type { RecapResponse } from "../protocol/recap";

const { useAwayRecap, AWAY_MS, resetRecapFiredForTests } = await import("./useAwayRecap");
type RecapRoot = import("./useAwayRecap").RecapRoot;

/** A root element that is drawing: laid out (`offsetParent`) and a real box. */
const ON_SCREEN: RecapRoot = {
  offsetParent: {},
  getBoundingClientRect: () => ({ width: 640, height: 480 }),
};
/** A hidden tab or a held-off pane: still mounted, `display:none` above it. */
const OFF_SCREEN: RecapRoot = {
  offsetParent: null,
  getBoundingClientRect: () => ({ width: 640, height: 480 }),
};

type Handler = () => void;

/** A window/document pair that records what is bound and can fire it. */
function fakeEnv() {
  const bound = new Map<string, Set<Handler>>();
  const on = (type: string, fn: Handler) => {
    if (!bound.has(type)) bound.set(type, new Set());
    bound.get(type)!.add(fn);
  };
  const off = (type: string, fn: Handler) => void bound.get(type)?.delete(fn);
  const doc = { hidden: false, addEventListener: on, removeEventListener: off };
  return {
    view: { addEventListener: on, removeEventListener: off } as unknown as Window,
    doc,
    fire(type: string) {
      for (const fn of [...(bound.get(type) ?? [])]) fn();
    },
    /** The tab goes away and comes back, `awayFor` ms apart on the fake clock. */
    async leaveAndReturn(clock: { t: number }, awayFor: number) {
      doc.hidden = true;
      await act(async () => this.fire("visibilitychange"));
      clock.t += awayFor;
      doc.hidden = false;
      await act(async () => this.fire("visibilitychange"));
      await act(async () => {});
    },
  };
}

const ANSWER: RecapResponse = {
  text: "You are porting the chat to React; the recap fold is next.",
  for_uuid: "u1",
  at: "2026-09-11T10:00:00Z",
};

const mounted: ReactTestRenderer[] = [];
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
  // The one-per-return stamp is MODULE state, so it outlives a mount and would
  // otherwise carry one test's fire into the next one's fake clock.
  resetRecapFiredForTests();
});

interface Knobs {
  forUuid?: string | null;
  running?: boolean;
  draft?: string;
  enabled?: boolean;
  awayMs?: number;
  answer?: RecapResponse | Error;
  /** The mount's box. Visible unless a test says otherwise. */
  root?: RecapRoot | null;
}

/** `count` is how many chats are mounted on this page — one, unless the test is
 *  about the page rather than the chat. */
function harness(initial: Knobs = {}, count = 1) {
  const env = fakeEnv();
  const clock = { t: 1_000_000 };
  const calls: Array<[string, string, string]> = [];
  let knobs: Knobs = { ...initial };
  let out: ReturnType<typeof useAwayRecap> | null = null;

  /** Set by `hang()`: a promise the test resolves itself. */
  let pending: Promise<RecapResponse> | null = null;
  const read = (file: string, sessionId: string, forUuid: string) => {
    calls.push([file, sessionId, forUuid]);
    if (pending) return pending;
    const a = knobs.answer ?? ANSWER;
    return a instanceof Error ? Promise.reject(a) : Promise.resolve(a);
  };

  function Hook() {
    out = useAwayRecap({
      file: "/w/app.py",
      sessionId: "s1",
      forUuid: knobs.forUuid === undefined ? "u1" : knobs.forUuid,
      running: knobs.running ?? false,
      hasDraft: () => (knobs.draft ?? "").trim().length > 0,
      enabled: knobs.enabled ?? true,
      ...(knobs.awayMs === undefined ? {} : { awayMs: knobs.awayMs }),
      root: () => (knobs.root === undefined ? ON_SCREEN : knobs.root),
      fetchRecap: read as never,
      view: env.view,
      doc: env.doc,
      now: () => clock.t,
    });
    return null;
  }
  function Probe() {
    return createElement(
      "div",
      null,
      ...Array.from({ length: count }, (_, i) => createElement(Hook, { key: i })),
    );
  }

  let r!: ReactTestRenderer;
  act(() => {
    r = create(createElement(Probe));
  });
  mounted.push(r);
  return {
    calls,
    clock,
    env,
    hang(p: Promise<RecapResponse>) {
      pending = p;
    },
    recap: () => out!.recap,
    dismiss: () => act(() => out!.dismiss()),
    async set(next: Knobs) {
      knobs = { ...knobs, ...next };
      await act(async () => r.update(createElement(Probe)));
    },
    /** Away for `ms` (default: comfortably over the threshold), then back. */
    away(ms = AWAY_MS + 1) {
      return env.leaveAndReturn(clock, ms);
    },
  };
}

test("under the threshold buys nothing; over it fetches once and shows the fold", async () => {
  const h = harness();
  await h.away(AWAY_MS - 1);
  expect(h.calls).toEqual([]);
  expect(h.recap()).toBe(null);

  await h.away();
  expect(h.calls).toEqual([["/w/app.py", "s1", "u1"]]);
  expect(h.recap()?.text).toBe(ANSWER.text);

  // The SAME position is never asked about twice, however often the reader
  // comes and goes.
  await h.away();
  expect(h.calls.length).toBe(1);
});

test("a draft, a running turn and the pref off each hold the call back", async () => {
  const h = harness({ draft: "wait, one more thing" });
  await h.away();
  expect(h.calls).toEqual([]);

  await h.set({ draft: "", running: true });
  await h.away();
  expect(h.calls).toEqual([]);

  await h.set({ running: false, enabled: false });
  await h.away();
  expect(h.calls).toEqual([]);

  // …and with every gate lifted, the same absence does fetch — so the three
  // above are refusals rather than a hook that never worked.
  await h.set({ enabled: true });
  await h.away();
  expect(h.calls.length).toBe(1);
});

test("a dismissed position is never asked about again", async () => {
  const h = harness();
  await h.away();
  expect(h.recap()?.text).toBe(ANSWER.text);
  h.dismiss();
  expect(h.recap()).toBe(null);
  await h.away();
  expect(h.calls.length).toBe(1);
});

test("empty text renders nothing — and is not an error to retry", async () => {
  const h = harness({ answer: { text: "", for_uuid: "u1", at: "" } });
  await h.away();
  expect(h.calls.length).toBe(1);
  expect(h.recap()).toBe(null);
  // "Nothing to show" is a real answer for this position: asking again would
  // buy the same nothing at the same price.
  await h.away();
  expect(h.calls.length).toBe(1);
});

test("a failure is retried once and then given up on; a new position re-arms", async () => {
  const h = harness({ answer: new Error("boom") });
  await h.away();
  await h.away();
  expect(h.calls.length).toBe(2);
  // MAX_FAILURES reached — the third return asks nothing, even though the
  // position is untouched.
  await h.away();
  expect(h.calls.length).toBe(2);
});

test("a message sent while the model was thinking drops the answer", async () => {
  // The read hangs for the whole of the send, which is the real shape of it:
  // generation takes ~12s and a reader who came back to type does not wait.
  let release!: (r: RecapResponse) => void;
  const slow = new Promise<RecapResponse>((res) => {
    release = res;
  });
  const h = harness({ answer: undefined });
  h.hang(slow);
  await h.away();
  expect(h.calls.length).toBe(1);
  expect(h.recap()).toBe(null); // silent while it thinks — no loading state

  // The reader sends: a new user turn, hence a new anchor.
  await h.set({ forUuid: "u2" });
  await act(async () => {
    release(ANSWER);
    await slow;
  });
  expect(h.recap()).toBe(null);
});

test("`awayMs` is the knob a test (or a browser check) turns down", async () => {
  const h = harness({ awayMs: 0 });
  await h.away(0);
  expect(h.calls.length).toBe(1);
});

test("a chat that is not on screen is not a chat anyone can read a fold in", async () => {
  // The tasks wall's cards and the explorer's held-off pane are both MOUNTED
  // and both hear the same `focus`; what they are not is visible.
  const h = harness({ root: OFF_SCREEN });
  await h.away();
  expect(h.calls).toEqual([]);

  // A ref that has not attached yet is the same answer — a mount drawing
  // nothing is not the chat in front of the reader.
  await h.set({ root: null });
  await h.away();
  expect(h.calls).toEqual([]);

  // Laid out but given no room (a pane at zero width) — still nothing to read.
  await h.set({ root: { offsetParent: {}, getBoundingClientRect: () => ({ width: 0, height: 0 }) } });
  await h.away();
  expect(h.calls).toEqual([]);

  // …and on screen, the same absence does fetch.
  await h.set({ root: ON_SCREEN });
  await h.away();
  expect(h.calls.length).toBe(1);
});

test("two chats on one page, one return, ONE model call", async () => {
  // The split: the explorer's content pane and its `?_side=claude` sidebar are
  // both primary and both visible. One reader came back, so one recap is
  // fetched and the other hook stands down (SAME_RETURN_MS).
  const h = harness({}, 2);
  await h.away();
  expect(h.calls.length).toBe(1);
});
