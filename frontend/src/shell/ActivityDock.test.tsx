// D664's retire-toast diffing — `retiredEngines`, exported from
// ActivityDock.tsx purely so this suite can exercise it directly (C9: D664
// shipped on this branch with no test at all, and C5 is exactly the defect
// that gap let through). No render, no poll, no `window`/`document`: this
// is the same pure-function-with-a-test split `jobs.ts`/`repo-updates-lib.ts`
// use for the parts of a dock that are wrong in ways a screenshot won't show.
//
// The suite below this ADDS a render-level harness for the one thing this
// file's pure functions cannot reach: `onJobPopup`'s wiring through the real,
// default-exported `ActivityDock`, `DownloadManager`'s `useJobs` poll and
// `popupTick`'s own first-tick rule, all mounted together. `globalThis.fetch`
// is stubbed directly (not `mock.module("@platform/lib/api")`) for the same
// reason `DownloadManager.test.tsx`'s own header gives: a module mock
// replaces the specifier for the whole bun process, not just this file, and
// has contaminated unrelated suites before. `window.setTimeout`/`clearTimeout`
// are captured rather than left real, so a poll cycle advances on command
// instead of a real wall-clock wait.
import { expect, test } from "bun:test";
import { act, create } from "react-test-renderer";

// ActivityDock.tsx now renders DownloadManager's JobRow, which imports
// router.ts (a terminal row's rowClick dispatches through `navigateToJobPage`)
// — router.ts reads `location` at module scope, so the shim has to land
// before the import, via a dynamic import exactly like JobRow.test.tsx's own.
import { installDomShim } from "@platform/lib/testDomShim";
import type { RunningEngine } from "@platform/lib/api";
import type { Job, JobsSnapshot } from "@platform/lib/jobs";

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
// `/api/jobs` round trip has even gone out — a placeholder, not an
// observation. Forwarding that placeholder to `onJobsReported` used to spend
// `popupTick`'s first-tick seeding on an empty snapshot, so the poll's actual
// first real read (which can already contain terminal jobs left over from a
// previous session) read as every one of them turning terminal for the very
// first time, and popped a card for each. `DownloadManagerView`'s `loaded`
// gate is what this suite is proving: nothing pops until a genuine response
// has landed, and once one has, a job crossing into terminal on the NEXT
// poll still pops exactly as before.

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
    ...over,
  };
}

function snapshot(jobs: Job[]): JobsSnapshot {
  return { jobs, now: Date.now() / 1000 };
}

function okResponse(data: unknown): Response {
  return { ok: true, status: 200, json: async () => data } as unknown as Response;
}

/** Captures every `window.setTimeout` call instead of letting it run for
 *  real, so a poll cycle advances on command (`fireAll`) rather than a real
 *  wall-clock wait — the render-level twin of `hook-harness.ts`'s `Clock`,
 *  scoped to just the one member `useJobs`/`useRunningEngines` actually call.
 *
 *  Bun runs every test file in one process, so overwriting `window.setTimeout`
 *  here without ever putting the real one back left it stubbed for every
 *  suite that ran after this one in the same invocation — their timers went
 *  into `pending` too, with nothing left around to drain it, and simply never
 *  fired. `restore()` puts the pre-capture functions back; every caller below
 *  calls it in a `finally`, the same way `globalThis.fetch` is restored just
 *  above. */
function captureTimers(): { fireAll: () => void; restore: () => void } {
  const pending = new Map<number, () => void>();
  let nextId = 1;
  const win = (globalThis as Record<string, unknown>).window as Record<string, unknown>;
  const realSetTimeout = win.setTimeout;
  const realClearTimeout = win.clearTimeout;
  win.setTimeout = ((fn: () => void) => {
    const id = nextId++;
    pending.set(id, fn);
    return id;
  }) as typeof globalThis.setTimeout;
  win.clearTimeout = ((id: number) => void pending.delete(id)) as typeof globalThis.clearTimeout;
  return {
    fireAll: () => {
      const due = [...pending.values()];
      pending.clear();
      for (const fn of due) fn();
    },
    restore: () => {
      win.setTimeout = realSetTimeout;
      win.clearTimeout = realClearTimeout;
    },
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
  const timers = captureTimers();
  const jobsResponses = [snapshot([job({ id: "old", state: "done" })])];
  let jobsCalls = 0;
  const realFetch = globalThis.fetch;
  globalThis.fetch = (async (url: string) => {
    if (url === "/api/jobs") {
      return okResponse(jobsResponses[Math.min(jobsCalls++, jobsResponses.length - 1)]);
    }
    if (url === "/api/engines/running") return okResponse({ engines: [] });
    throw new Error(`unstubbed fetch: ${url}`);
  }) as typeof fetch;

  const popped: Job[] = [];
  try {
    await act(async () => {
      create(<ActivityDock onJobPopup={(j) => popped.push(j)} />);
    });
    await flush();

    // The very first `/api/jobs` read already found "old" done — exactly the
    // backlog a page load or a refresh would see. Nothing should have popped.
    expect(popped).toEqual([]);

    // A second, unchanged poll (the id's own row is still there, still done)
    // must not pop it either — it is not a NEW terminal event.
    timers.fireAll();
    await flush();
    expect(popped).toEqual([]);
  } finally {
    globalThis.fetch = realFetch;
    timers.restore();
  }
});

test("a job crossing into terminal AFTER the first real read still pops", async () => {
  const timers = captureTimers();
  const jobsResponses = [
    snapshot([job({ id: "a", state: "running" })]),
    snapshot([job({ id: "a", state: "done", finished_at: 500 })]),
  ];
  let jobsCalls = 0;
  const realFetch = globalThis.fetch;
  globalThis.fetch = (async (url: string) => {
    if (url === "/api/jobs") {
      return okResponse(jobsResponses[Math.min(jobsCalls++, jobsResponses.length - 1)]);
    }
    if (url === "/api/engines/running") return okResponse({ engines: [] });
    throw new Error(`unstubbed fetch: ${url}`);
  }) as typeof fetch;

  const popped: Job[] = [];
  try {
    await act(async () => {
      create(<ActivityDock onJobPopup={(j) => popped.push(j)} />);
    });
    await flush();
    // First read: "a" is still running — nothing terminal yet, nothing popped.
    expect(popped).toEqual([]);

    // The poll scheduled after that first read is what carries the SECOND,
    // real response — this is the tick under test, not the first-tick seed.
    timers.fireAll();
    await flush();
    expect(popped.map((j) => j.id)).toEqual(["a"]);
  } finally {
    globalThis.fetch = realFetch;
    timers.restore();
  }
});
