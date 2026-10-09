// D664's retire-toast diffing — `retiredEngines`, exported from
// ActivityDock.tsx purely so this suite can exercise it directly (C9: D664
// shipped on this branch with no test at all, and C5 is exactly the defect
// that gap let through). No render, no poll, no `window`/`document`: this
// is the same pure-function-with-a-test split `jobs.ts`/`repo-updates-lib.ts`
// use for the parts of a dock that are wrong in ways a screenshot won't show.
//
// The suite below this ADDS a render-level harness for the one thing this
// file's pure functions cannot reach: `onJobPopup`'s wiring through the real,
// default-exported `ActivityDock`, `DownloadManager`'s `useJobs` subscription
// and `popupTick`'s own first-tick rule, all mounted together. The events
// client is swapped for a scripted one (`setEventsClientForTests`, not
// `mock.module("@platform/lib/api")`) for the same reason
// `DownloadManager.test.tsx`'s own header gives: a module mock replaces the
// specifier for the whole bun process, not just this file, and has
// contaminated unrelated suites before. The fake answers the `jobs` and
// `engines.running` topics from a script and lets a test push the NEXT
// snapshot on command, which is what a frame from the server looks like.
import { expect, test } from "bun:test";
import { act, create } from "react-test-renderer";

// ActivityDock.tsx now renders DownloadManager's JobRow, which imports
// router.ts (a terminal row's rowClick dispatches through `navigateToJobPage`)
// — router.ts reads `location` at module scope, so the shim has to land
// before the import, via a dynamic import exactly like JobRow.test.tsx's own.
import { installDomShim } from "@platform/lib/testDomShim";
import type { RunningEngine } from "@platform/lib/api";
import type { Job, JobsSnapshot } from "@platform/lib/jobs";
import { setEventsClientForTests } from "@platform/lib/events";

installDomShim();
const ActivityDockModule = await import("@shell/ActivityDock");
const { retiredEngines } = ActivityDockModule;
const ActivityDock = ActivityDockModule.default;

function engine(over: Partial<RunningEngine> = {}): RunningEngine {
  return {
    engine_id: "e1",
    pid: 1,
    version: "1.0.0",
    folder: "/apps/thing",
    module: "",
    uptime_s: 120,
    idle_timeout_s: 900,
    idle_for_s: 0,
    busy: false,
    ...over,
  };
}

test("an engine missing from the next snapshot, with no stopping marker, is a genuine retirement", () => {
  const e = engine();
  const stopping = new Map<string, number>();
  const retired = retiredEngines([e], [], stopping, 1_000_000);
  expect(retired).toEqual([e]);
});

test("an engine still present in the next snapshot never retires, marker or not", () => {
  const e = engine();
  const stopping = new Map<string, number>([[e.engine_id, 999_000]]);
  const retired = retiredEngines([e], [e], stopping, 1_000_000);
  expect(retired).toEqual([]);
  // The marker is untouched — the engine never disappeared, so there is
  // nothing to consume it yet.
  expect(stopping.has(e.engine_id)).toBe(true);
});

test("a fresh stopping marker suppresses the toast for the engine it names", () => {
  const e = engine();
  const stopping = new Map<string, number>([[e.engine_id, 1_000_000]]);
  // Well within STOPPING_GRACE_MS (30s) of the marker.
  const retired = retiredEngines([e], [], stopping, 1_005_000);
  expect(retired).toEqual([]);
  // Consumed on the tick that checked it, whether or not it suppressed
  // anything — a marker is spent the moment its window is evaluated.
  expect(stopping.has(e.engine_id)).toBe(false);
});

test("C5: a stopping marker past its grace window no longer swallows a later, genuine retirement", () => {
  // `stopEngine()` rejected, or the engine is a `main =` app `restart()`
  // revived — either way nothing ever consumed the marker at the time, and
  // it sat in the map. Before the fix this permanently ate the id's next
  // real idle-retirement, however much later that happened. The grace
  // window bounds how long a click can plausibly still be resolving for.
  const e = engine();
  const stopping = new Map<string, number>([[e.engine_id, 0]]);
  // Long past STOPPING_GRACE_MS (30s) since the marker was set.
  const retired = retiredEngines([e], [], stopping, 60_000);
  expect(retired).toEqual([e]);
  expect(stopping.has(e.engine_id)).toBe(false);
});

test("only the engines actually missing are reported — a mixed snapshot", () => {
  const stays = engine({ engine_id: "stays" });
  const goesQuiet = engine({ engine_id: "goes-quiet" });
  const userStopped = engine({ engine_id: "user-stopped" });
  const stopping = new Map<string, number>([["user-stopped", 1_000_000]]);
  const retired = retiredEngines([stays, goesQuiet, userStopped], [stays], stopping, 1_001_000);
  expect(retired).toEqual([goesQuiet]);
});

// ---------------------------------------------------- onJobPopup's own wiring
//
// `DownloadManager`'s `useJobs` starts with `jobs: []` before its first
// `jobs` snapshot has even landed — a placeholder, not an observation.
// Forwarding that placeholder to `onJobsReported` used to spend
// `popupTick`'s first-tick seeding on an empty snapshot, so the first real
// snapshot (which can already contain terminal jobs left over from a
// previous session) read as every one of them turning terminal for the very
// first time, and popped a card for each. `DownloadManagerView`'s `loaded`
// gate is what this suite is proving: nothing pops until a genuine snapshot
// has landed, and once one has, a job crossing into terminal on the NEXT
// snapshot still pops exactly as before.

function job(over: Partial<Job> = {}): Job {
  return {
    id: "sys:ai-image:x",
    title: "a red fox",
    detail: "",
    model: "",
    kind: "task",
    state: "done",
    done: null,
    total: null,
    total_scope: "phase",
    total_estimated: false,
    unit: "",
    message: "",
    page: "",
    source: "",
    origin: "",
    owner: "server",
    cancellable: false,
    cancel_requested: false,
    started_at: 0,
    updated_at: 0,
    finished_at: 0,
    stalled: false,
    waiting_for: "",
    tier: "trail",
    group: over.id ?? "sys:ai-image:x",
    ...over,
  };
}

function snapshot(jobs: Job[]): JobsSnapshot {
  return { jobs, now: Date.now() / 1000 };
}

type Frame = (snap: unknown, delta: unknown, meta: Record<string, unknown>) => void;

/** A scripted events client: `subscribe` answers each topic with its first
 *  scripted snapshot at once (the bus always answers a subscribe with a
 *  snapshot), remembers the callback, and `push` hands a later snapshot to
 *  every live subscriber of that topic — the render-level twin of
 *  `jobs.test.ts`'s fake subscribe, scoped to the two topics the dock and
 *  its `DownloadManager` actually follow. `resyncs` counts `refresh()`
 *  calls so a test can prove a refresh is a resync of the subscription and
 *  never a fetch. */
function fakeEvents(first: Record<string, unknown>): {
  push: (topic: string, snap: unknown) => void;
  pushError: (topic: string) => void;
  subscriptions: (topic: string) => number;
  resyncs: string[];
  restore: () => void;
} {
  const subs = new Map<string, Set<Frame>>();
  const resyncs: string[] = [];
  setEventsClientForTests({
    subscribe: ((topic: string, _params: unknown, cb: Frame) => {
      if (!(topic in first)) throw new Error(`unscripted topic: ${topic}`);
      let set = subs.get(topic);
      if (!set) subs.set(topic, (set = new Set()));
      set.add(cb);
      cb(first[topic], null, { gen: null });
      return () => {
        set.delete(cb);
      };
    }) as never,
    resync: (topic: string) => {
      resyncs.push(topic);
      return true;
    },
  });
  return {
    push: (topic, snap) => {
      for (const cb of subs.get(topic) ?? []) cb(snap, null, { gen: null });
    },
    pushError: (topic) => {
      for (const cb of subs.get(topic) ?? []) cb(null, null, { error: "down", status: 500 });
    },
    subscriptions: (topic) => subs.get(topic)?.size ?? 0,
    resyncs,
    restore: () => setEventsClientForTests(null),
  };
}

async function flush(): Promise<void> {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });
}

test("a page load's already-terminal jobs seed silently — onJobPopup never fires for them", async () => {
  const bus = fakeEvents({
    jobs: snapshot([job({ id: "old", state: "done" })]),
    "engines.running": { engines: [] },
  });
  const popped: Job[] = [];
  try {
    await act(async () => {
      create(<ActivityDock onJobPopup={(j) => popped.push(j)} />);
    });
    await flush();

    // The very first `jobs` snapshot already found "old" done — exactly the
    // backlog a page load or a refresh would see. Nothing should have popped.
    expect(popped).toEqual([]);

    // A second, unchanged snapshot (the id's own row is still there, still
    // done) must not pop it either — it is not a NEW terminal event.
    await act(async () => {
      bus.push("jobs", snapshot([job({ id: "old", state: "done" })]));
    });
    await flush();
    expect(popped).toEqual([]);
  } finally {
    bus.restore();
  }
});

test("a job crossing into terminal AFTER the first real snapshot still pops", async () => {
  // "error", not "done" (2026-09-23, D888): a successful `done` job never
  // pops any more — only error/cancelled does — so the real-wiring proof of
  // "a later-terminal job still pops" needs a terminal state that actually
  // still pops.
  const bus = fakeEvents({
    jobs: snapshot([job({ id: "a", state: "running" })]),
    "engines.running": { engines: [] },
  });
  const popped: Job[] = [];
  try {
    await act(async () => {
      create(<ActivityDock onJobPopup={(j) => popped.push(j)} />);
    });
    await flush();
    // First snapshot: "a" is still running — nothing terminal yet, nothing popped.
    expect(popped).toEqual([]);

    // The frame the server pushes when the job reports its failure — this
    // is the snapshot under test, not the first-tick seed.
    await act(async () => {
      bus.push("jobs", snapshot([job({ id: "a", state: "error", finished_at: 500 })]));
    });
    await flush();
    expect(popped.map((j) => j.id)).toEqual(["a"]);
  } finally {
    bus.restore();
  }
});

// D-C's own end-to-end wiring (SPEC-quiet-notifications.md §3): a
// MULTI-member group pops on FAILURE, routed through `groupPopupTick` rather
// than `popupTick`, but landing on the same `onJobPopup` callback — this
// proves that pop source actually reaches the caller, not just the pure
// function in isolation (already covered in `jobs.test.ts`). A START pop
// used to exist here too but was removed (2026-09-23, D888): the Activity
// chip's own progress indicator already signals "something is running", so
// a start card was redundant.
test("a multi-member group pops once on FAILURE, through the real ActivityDock wiring", async () => {
  const bus = fakeEvents({
    jobs: snapshot([
      job({ id: "sys:g:a", state: "running", group: "sys:g", started_at: 100 }),
      job({ id: "sys:g:b", state: "running", group: "sys:g", started_at: 100 }),
    ]),
    "engines.running": { engines: [] },
  });
  const popped: Job[] = [];
  try {
    await act(async () => {
      create(<ActivityDock onJobPopup={(j) => popped.push(j)} />);
    });
    await flush();
    expect(popped).toEqual([]); // first snapshot seeds silently, both members running

    await act(async () => {
      bus.push(
        "jobs",
        snapshot([
          job({ id: "sys:g:a", state: "error", group: "sys:g", started_at: 100, finished_at: 900 }),
          job({ id: "sys:g:b", state: "running", group: "sys:g", started_at: 100 }),
        ]),
      );
    });
    await flush();
    // "sys:g:a" just failed — a FAILURE pops even though its sibling is
    // still running and the group as a whole is not yet terminal.
    expect(popped.map((j) => j.id)).toEqual(["sys:g:a"]);
  } finally {
    bus.restore();
  }
});

test("a multi-member group does NOT pop on an ordinary member completion, through the real wiring", async () => {
  const bus = fakeEvents({
    jobs: snapshot([
      job({ id: "sys:g:a", state: "running", group: "sys:g", started_at: 100 }),
      job({ id: "sys:g:b", state: "running", group: "sys:g", started_at: 100 }),
    ]),
    "engines.running": { engines: [] },
  });
  const popped: Job[] = [];
  try {
    await act(async () => {
      create(<ActivityDock onJobPopup={(j) => popped.push(j)} />);
    });
    await flush();
    await act(async () => {
      bus.push(
        "jobs",
        snapshot([
          job({ id: "sys:g:a", state: "done", group: "sys:g", started_at: 100, finished_at: 900 }),
          job({ id: "sys:g:b", state: "running", group: "sys:g", started_at: 100 }),
        ]),
      );
    });
    await flush();
    expect(popped).toEqual([]);
  } finally {
    bus.restore();
  }
});

// ------------------------------------------------ the engines subscription
//
// `useRunningEngines` is the dock's own `engines.running` subscription: one
// per mounted dock (the client refcounts further sharing), answered by a
// snapshot, diffed against the previous one for D664's retire toast, and
// resynced — never fetched — by `refresh()`. Driven through the real
// `ActivityDock` so the wiring from frame to `DownloadManager`'s Background
// tasks section is what is under test, not a hook in isolation.

const DownloadManagerType = (await import("@platform/ui/DownloadManager")).default;

/** The engine ids the dock is currently handing `DownloadManager` to draw. */
function enginesShown(root: ReturnType<typeof create>["root"]): string[] {
  const dm = root.findByType(DownloadManagerType);
  return (dm.props as { engines: { engines: RunningEngine[] } }).engines.engines.map((e) => e.engine_id);
}

test("the dock holds exactly one engines.running subscription, seeded by its first snapshot and moved by the next", async () => {
  const bus = fakeEvents({
    jobs: snapshot([]),
    "engines.running": { engines: [engine({ engine_id: "e1" })] },
  });
  const { _resetNotificationsForTest, getPopupNotification } = await import("@platform/lib/notifications");
  _resetNotificationsForTest();
  try {
    let r!: ReturnType<typeof create>;
    await act(async () => {
      r = create(<ActivityDock />);
    });
    await flush();
    expect(bus.subscriptions("engines.running")).toBe(1);
    expect(enginesShown(r.root)).toEqual(["e1"]);

    await act(async () => {
      bus.push("engines.running", { engines: [engine({ engine_id: "e1" }), engine({ engine_id: "e2" })] });
    });
    await flush();
    expect(enginesShown(r.root)).toEqual(["e1", "e2"]);
    // Still the one subscription — a new snapshot is a frame, not a resubscribe.
    expect(bus.subscriptions("engines.running")).toBe(1);
    // The first snapshot seeded silently: nothing "retired" on mount.
    expect(getPopupNotification()).toBeNull();

    // D664: an engine missing from the NEXT snapshot, with no Stop of the
    // user's own behind it, is an idle retirement and draws its toast.
    await act(async () => {
      bus.push("engines.running", { engines: [engine({ engine_id: "e1" })] });
    });
    await flush();
    expect(enginesShown(r.root)).toEqual(["e1"]);
    expect(getPopupNotification()?.title).toContain("retired (idle)");
    _resetNotificationsForTest();

    // A refused frame leaves the last snapshot standing.
    await act(async () => {
      bus.pushError("engines.running");
    });
    await flush();
    expect(enginesShown(r.root)).toEqual(["e1"]);

    await act(async () => {
      r.unmount();
    });
    expect(bus.subscriptions("engines.running")).toBe(0);
  } finally {
    _resetNotificationsForTest();
    bus.restore();
  }
});

test("stopping an engine marks it, asks the bus to resync, and the next snapshot dropping it draws no retire toast", async () => {
  const bus = fakeEvents({
    jobs: snapshot([]),
    "engines.running": { engines: [engine({ engine_id: "e1" })] },
  });
  // `stopEngine` POSTs; answer it so the handler reaches `refresh()`.
  const realFetch = globalThis.fetch;
  globalThis.fetch = (async () =>
    ({ ok: true, status: 200, json: async () => ({ ok: true }) }) as unknown as Response) as unknown as typeof fetch;
  const { _resetNotificationsForTest, getPopupNotification } = await import("@platform/lib/notifications");
  _resetNotificationsForTest();
  try {
    let r!: ReturnType<typeof create>;
    await act(async () => {
      r = create(<ActivityDock />);
    });
    await flush();
    const dm = r.root.findByType(DownloadManagerType);
    await act(async () => {
      await (dm.props as { engines: { onStop: (id: string) => Promise<void> } }).engines.onStop("e1");
    });
    await flush();
    expect(bus.resyncs).toEqual(["engines.running"]);
    await act(async () => {
      bus.push("engines.running", { engines: [] });
    });
    await flush();
    expect(enginesShown(r.root)).toEqual([]);
    // The marker consumed the drop: no "retired (idle)" toast for a user's own Stop.
    expect(getPopupNotification()).toBeNull();
  } finally {
    globalThis.fetch = realFetch;
    _resetNotificationsForTest();
    bus.restore();
  }
});
