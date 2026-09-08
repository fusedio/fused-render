// The standing watch (D415): the pure follow rule, the order in which the two
// questions are asked, and the four triggers — including the one that is
// deliberately SKIPPED while nobody is looking.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { describe, expect, test } from "bun:test";
import { createLiveWatch, followDecision, LIVE_WATCH_MS } from "./watch";
import type { TranscriptStat } from "../protocol/types";

const mark = (over: Partial<TranscriptStat> = {}): TranscriptStat => ({
  path: "/p/s1.jsonl",
  mtime: 100,
  size: 4000,
  ...over,
});

describe("followDecision", () => {
  test("no watermark, or no file, is not a reason to do anything", () => {
    expect(followDecision(null, { exists: true, mtime: 200, size: 1, running: true }, 0)).toEqual({
      refresh: false,
      running: false,
    });
    expect(followDecision(mark(), null, 0)).toEqual({ refresh: false, running: false });
    // A transcript whose first turn is still being written answers `exists:
    // false` rather than 404 — and there is nothing to compare against.
    expect(
      followDecision(mark(), { exists: false, mtime: 0, size: 0, running: false }, 0),
    ).toEqual({ refresh: false, running: false });
  });

  test("THE PAIR, not mtime alone: a coarse clock can put two appends in one tick", () => {
    const same = { exists: true, mtime: 100, size: 4000, running: false };
    expect(followDecision(mark(), same, 0).refresh).toBe(false);
    expect(followDecision(mark(), { ...same, mtime: 101 }, 0).refresh).toBe(true);
    expect(followDecision(mark(), { ...same, size: 4001 }, 0).refresh).toBe(true);
  });

  test("OUR OWN ECHO is not somebody else's turn", () => {
    // The transcript is freshest right after a run this frame streamed, and
    // `session_liveness` still calls the session running for a few seconds
    // afterwards. Both are in EPOCH SECONDS, which is what `os.stat` reports.
    const probe = { exists: true, mtime: 500, size: 9, running: true };
    expect(followDecision(mark(), probe, 500).running).toBe(false);
    expect(followDecision(mark(), probe, 501).running).toBe(false);
    expect(followDecision(mark(), probe, 499).running).toBe(true);
    // ...and a file that moved with nothing running is a refresh and no line.
    expect(followDecision(mark(), { ...probe, running: false }, 0)).toEqual({
      refresh: true,
      running: false,
    });
  });
});

// ---- the lap ---------------------------------------------------------------

interface Rig {
  sessionId: string;
  busy: boolean;
  probe: { exists: boolean; mtime: number; size: number; running: boolean };
  markValue: TranscriptStat | null;
  ownEnd: number;
  /** `!!activeRun` alone. Defaults to whatever `busy` says, which is what a
   *  caller that cannot tell the two apart gets. */
  activeRun?: boolean;
  adopts: string[];
  refreshes: string[];
  external: boolean[];
  livenessCalls: string[];
  /** A liveness read nobody has answered yet, for the across-the-await tests. */
  hold?: { resolve: () => void };
}

function rig(over: Partial<Rig> = {}) {
  const state: Rig = {
    sessionId: "s1",
    busy: false,
    probe: { exists: true, mtime: 200, size: 5000, running: true },
    markValue: mark(),
    ownEnd: 0,
    adopts: [],
    refreshes: [],
    external: [],
    livenessCalls: [],
    ...over,
  };
  const timers: (() => void)[] = [];
  const listeners: Record<string, ((ev: unknown) => void)[]> = {};
  const target = (prefix: string) => ({
    addEventListener(type: string, fn: (ev: never) => void) {
      (listeners[prefix + type] ||= []).push(fn as (ev: unknown) => void);
    },
    removeEventListener(type: string, fn: (ev: never) => void) {
      const list = listeners[prefix + type] || [];
      const at = list.indexOf(fn as (ev: unknown) => void);
      if (at >= 0) list.splice(at, 1);
    },
  });
  const watch = createLiveWatch({
    sessionId: () => state.sessionId,
    busy: () => state.busy,
    hasActiveRun: () => (state.activeRun === undefined ? state.busy : state.activeRun),
    adopt: (id) => {
      state.adopts.push(id);
      return Promise.resolve();
    },
    transcriptMark: () => state.markValue,
    ownRunEndedAt: () => state.ownEnd,
    liveness: (path) => {
      state.livenessCalls.push(path);
      if (state.hold) {
        return new Promise((res) => {
          state.hold!.resolve = () => res(state.probe);
        });
      }
      return Promise.resolve(state.probe);
    },
    refreshHistory: (id) => {
      state.refreshes.push(id);
      return Promise.resolve();
    },
    setExternalWorking: (on) => state.external.push(on),
    activityKey: "fused-render:chat-activity",
    setInterval: (fn) => {
      timers.push(fn);
      return timers.length;
    },
    clearInterval: () => {},
    isHidden: () => hidden,
    win: target("win:"),
    doc: target("doc:"),
  });
  let hidden = false;
  return {
    state,
    watch,
    fireInterval: () => timers.forEach((fn) => fn()),
    setHidden: (v: boolean) => {
      hidden = v;
    },
    fire: (name: string, ev: unknown = {}) => {
      for (const fn of listeners[name] || []) fn(ev);
    },
    listeners,
  };
}

describe("liveWatchTick", () => {
  test("RUN DIRS FIRST: adopt, then the transcript, and only if nothing attached", async () => {
    const r = rig();
    await r.watch.tick();
    expect(r.state.adopts).toEqual(["s1"]);
    expect(r.state.livenessCalls).toEqual(["/p/s1.jsonl"]);
    expect(r.state.refreshes).toEqual(["s1"]);
    expect(r.state.external).toEqual([true]);
  });

  test("a run that attached during the adopt owns the chrome; the file is not asked", async () => {
    const r = rig();
    const inner = createLiveWatch({
      sessionId: () => "s1",
      busy: () => r.state.busy,
      adopt: () => {
        // The adopted run is live from here on.
        r.state.busy = true;
        return Promise.resolve();
      },
      transcriptMark: () => mark(),
      ownRunEndedAt: () => 0,
      liveness: (p) => {
        r.state.livenessCalls.push(p);
        return Promise.resolve(r.state.probe);
      },
      refreshHistory: () => Promise.resolve(),
      setExternalWorking: (on) => r.state.external.push(on),
      activityKey: "k",
    });
    await inner.tick();
    expect(r.state.livenessCalls).toEqual([]);
    expect(r.state.external).toEqual([]);
  });

  test("no session on screen: nothing is asked at all", async () => {
    // `live_run` with no session matches on the target alone, which would drag a
    // run belonging to some other chat about this folder onto this screen.
    const r = rig({ sessionId: "" });
    await r.watch.tick();
    expect(r.state.adopts).toEqual([]);
    expect(r.state.livenessCalls).toEqual([]);
  });

  test("a live run of our own is not looked past", async () => {
    const r = rig({ busy: true });
    await r.watch.tick();
    expect(r.state.adopts).toEqual([]);
  });

  test("no watermark yet: there is nothing to compare against", async () => {
    const r = rig({ markValue: null });
    await r.watch.tick();
    expect(r.state.adopts).toEqual(["s1"]);
    expect(r.state.livenessCalls).toEqual([]);
  });

  test("ONE LAP AT A TIME: a burst of triggers is one lookup, not three", async () => {
    const r = rig({ hold: { resolve: () => {} } });
    const first = r.watch.tick();
    await Promise.resolve();
    await r.watch.tick();
    await r.watch.tick();
    r.state.hold!.resolve();
    await first;
    expect(r.state.adopts).toEqual(["s1"]);
    expect(r.state.livenessCalls).toEqual(["/p/s1.jsonl"]);
  });

  test("A RENDER THAT LANDED WHILE WE ASKED discards the answer", async () => {
    const r = rig({ hold: { resolve: () => {} } });
    const lap = r.watch.tick();
    await Promise.resolve();
    await Promise.resolve();
    // `loadHistory` wrote a fresh watermark from its own pre-read stat, which is
    // newer than the probe now in flight.
    r.state.markValue = mark({ mtime: 300 });
    r.state.hold!.resolve();
    await lap;
    expect(r.state.refreshes).toEqual([]);
    expect(r.state.external).toEqual([]);
  });

  test("THE READER LEFT: a session change across the await outranks the answer", async () => {
    const r = rig({ hold: { resolve: () => {} } });
    const lap = r.watch.tick();
    await Promise.resolve();
    await Promise.resolve();
    r.state.sessionId = "s2";
    r.state.hold!.resolve();
    await lap;
    expect(r.state.refreshes).toEqual([]);
    expect(r.state.external).toEqual([]);
  });

  test("a failed stat leaves the transcript exactly as it rendered", async () => {
    const calls: string[] = [];
    const watch = createLiveWatch({
      sessionId: () => "s1",
      busy: () => false,
      adopt: () => Promise.resolve(),
      transcriptMark: () => mark(),
      ownRunEndedAt: () => 0,
      liveness: () => Promise.reject(new Error("400")),
      refreshHistory: (id) => {
        calls.push(id);
        return Promise.resolve();
      },
      setExternalWorking: (on) => calls.push("ext:" + on),
      activityKey: "k",
    });
    await watch.tick();
    expect(calls).toEqual([]);
  });

  test("REFRESH FIRST, THEN THE LINE: a line added before the render would be swept", async () => {
    const order: string[] = [];
    // ONE object, held: the watermark is compared by IDENTITY across the await,
    // so a getter minting a fresh one every call reads as "a render landed".
    const held = mark();
    const watch = createLiveWatch({
      sessionId: () => "s1",
      busy: () => false,
      adopt: () => Promise.resolve(),
      transcriptMark: () => held,
      ownRunEndedAt: () => 0,
      liveness: () => Promise.resolve({ exists: true, mtime: 900, size: 1, running: true }),
      refreshHistory: () => {
        order.push("refresh");
        return Promise.resolve();
      },
      setExternalWorking: (on) => order.push("external:" + on),
      activityKey: "k",
    });
    await watch.tick();
    expect(order).toEqual(["refresh", "external:true"]);
  });
});

describe("the triggers", () => {
  test("the interval is 5 s and is SKIPPED while the document is hidden", async () => {
    const r = rig();
    const stop = r.watch.start();
    expect(LIVE_WATCH_MS).toBe(5000);
    r.setHidden(true);
    r.fireInterval();
    await Promise.resolve();
    expect(r.state.adopts).toEqual([]);
    // Chrome nobody is looking at is worth nothing; becoming visible laps at
    // once, and the storage poke reaches a hidden tab anyway.
    r.setHidden(false);
    r.fireInterval();
    await Promise.resolve();
    await Promise.resolve();
    expect(r.state.adopts).toEqual(["s1"]);
    stop();
  });

  test("only the CHAT ACTIVITY key pokes: every other store's event is not news", async () => {
    const r = rig();
    const stop = r.watch.start();
    r.fire("win:storage", { key: "some-other-store" });
    await Promise.resolve();
    expect(r.state.adopts).toEqual([]);
    r.fire("win:storage", { key: "fused-render:chat-activity" });
    await Promise.resolve();
    await Promise.resolve();
    expect(r.state.adopts).toEqual(["s1"]);
    stop();
  });

  test("visibility and focus both lap, and the disarm really removes them", async () => {
    const r = rig();
    const stop = r.watch.start();
    r.fire("doc:visibilitychange");
    await Promise.resolve();
    await Promise.resolve();
    expect(r.state.adopts).toEqual(["s1"]);
    stop();
    // Every listener goes with the watch: a disposed chat must not keep polling
    // a session it is no longer showing.
    expect(r.listeners["win:focus"]).toEqual([]);
    expect(r.listeners["win:storage"]).toEqual([]);
    expect(r.listeners["doc:visibilitychange"]).toEqual([]);
    r.fire("win:focus");
    await Promise.resolve();
    await Promise.resolve();
    expect(r.state.adopts).toEqual(["s1"]);
  });
});

test("THE OFF EDGE ASKS ABOUT THE RUN, NOT THE SEND (T:17709)", async () => {
  // The last gate of a lap is crossed AFTER the refresh, and a send can land in
  // that window: `busy()` is then true (`activeRun || sending`) while nothing is
  // streaming. The external line has to come DOWN anyway, or it sits under the
  // user's own turn until their run ends. T gates this one check on
  // `!activeRun` and deliberately not on `sending`.
  const external: boolean[] = [];
  const build = (onRefresh: () => void, seen: boolean[]) => {
    const st = { busy: false, activeRun: false };
    // ONE object: the lap compares the watermark by IDENTITY after its await.
    const stable = mark();
    return {
      st,
      watch: createLiveWatch({
        sessionId: () => "s1",
        busy: () => st.busy,
        hasActiveRun: () => st.activeRun,
        adopt: () => Promise.resolve(),
        transcriptMark: () => stable,
        ownRunEndedAt: () => 0,
        // Moved, and nobody running in there — so this lap's job is to refresh
        // and then take the line down.
        liveness: () => Promise.resolve({ exists: true, mtime: 300, size: 6000, running: false }),
        refreshHistory: () => {
          onRefresh();
          return Promise.resolve();
        },
        setExternalWorking: (on) => seen.push(on),
        activityKey: "k",
      }),
    };
  };

  const a = build(() => {
    // The user's send lands while the history refresh is in flight.
    a.st.busy = true;
    a.st.activeRun = false;
  }, external);
  await a.watch.tick();
  expect(external).toEqual([false]);

  // A run this frame OWNS still takes the gate: that line is the honest one,
  // with a stop button and a token count this one cannot offer.
  const owned: boolean[] = [];
  const b = build(() => {
    b.st.busy = true;
    b.st.activeRun = true;
  }, owned);
  await b.watch.tick();
  expect(owned).toEqual([]);
});
