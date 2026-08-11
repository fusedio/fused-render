// Scheduling properties of the chunked scan. The point of this module is
// timing, so the tests drive a fake clock and a fake scheduler and assert what
// ran SYNCHRONOUSLY — the freeze was never a wrong answer, it was a right
// answer computed all at once.
import { expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import type { WalkEntry } from "@platform/lib/api";
import type { QueryTagged } from "@platform/lib/search-hold";
import type { SearchHit } from "@apps/explorer/listing/types";
import { startScanJob, type ScanJobDeps, type ScanJobSpec } from "@apps/explorer/listing/scan-job";

function last<T>(xs: T[]): T {
  return xs[xs.length - 1];
}

function corpus(n: number): WalkEntry[] {
  const out: WalkEntry[] = [];
  for (let i = 0; i < n; i++) out.push({ rel: `f${i}.ts`, is_dir: false, size: 1, mtime: 1 } as WalkEntry);
  return out;
}

function hit(entry: WalkEntry): SearchHit {
  return { entry, positions: [], score: 1, longestRun: 1, tier: 1, depth: 1 };
}

/** A fake event loop: nothing runs until the test drains it. */
function harness(overrides: Partial<ScanJobDeps> = {}) {
  const timers = new Map<number, { fn: () => void; ms: number }>();
  let nextId = 1;
  let clock = 0;
  const scanned: Array<[number, number]> = [];
  const published: Array<{ result: QueryTagged<SearchHit>; done: boolean }> = [];
  const progress: number[] = [];

  const deps: ScanJobDeps = {
    score: (_q, entries, from, _showHidden, to) => {
      scanned.push([from, to]);
      return entries.slice(from, to).map(hit);
    },
    sort: () => {},
    now: () => clock,
    setTimer: (fn, ms) => {
      const id = nextId++;
      timers.set(id, { fn, ms });
      return id;
    },
    clearTimer: (id) => void timers.delete(id),
    onPublish: (result, done) => void published.push({ result, done }),
    onProgress: (n) => void progress.push(n),
    ...overrides,
  };

  /** Run every pending timer once, advancing the clock by `advance` first. */
  const tick = (advance = 0) => {
    clock += advance;
    const due = [...timers.entries()];
    timers.clear();
    for (const [, t] of due) t.fn();
    return due.length;
  };
  const drain = (max = 200) => {
    let rounds = 0;
    while (timers.size && rounds++ < max) tick();
    return rounds;
  };

  return { deps, scanned, published, progress, tick, drain, timers, setClock: (c: number) => (clock = c) };
}

function spec(over: Partial<ScanJobSpec> & { entries: WalkEntry[] }): ScanJobSpec {
  return {
    q: "f",
    showHidden: true,
    from: 0,
    ranked: [],
    sliceSize: 20_000,
    immediateMax: 20_000,
    debounceMs: 150,
    commitMs: 280,
    ...over,
  };
}

test("a big corpus scores NOTHING synchronously", () => {
  // The bug: one keystroke, one full 200k scan, on the keystroke's own frame.
  const h = harness();
  startScanJob(spec({ entries: corpus(200_000) }), h.deps);
  expect(h.scanned).toEqual([]);
  expect(h.published).toEqual([]);
  expect(h.timers.size).toBe(1); // just the debounce, waiting
});

test("the debounce timer is the configured wait", () => {
  const h = harness();
  startScanJob(spec({ entries: corpus(200_000), debounceMs: 150 }), h.deps);
  expect([...h.timers.values()][0].ms).toBe(150);
});

test("a big corpus is scored in slices, never in one go", () => {
  const h = harness();
  startScanJob(spec({ entries: corpus(100_000), sliceSize: 20_000 }), h.deps);
  h.tick(); // fire the debounce -> first slice
  expect(h.scanned).toEqual([[0, 20_000]]);
  h.drain();
  expect(h.scanned).toEqual([
    [0, 20_000],
    [20_000, 40_000],
    [40_000, 60_000],
    [60_000, 80_000],
    [80_000, 100_000],
  ]);
  expect(last(h.published).done).toBe(true);
  expect(last(h.published).result.items).toHaveLength(100_000);
});

test("a small corpus skips the debounce and lands at once", () => {
  // The first keystroke of a fresh search on a normal folder must not wait.
  const h = harness();
  startScanJob(spec({ entries: corpus(500), immediateMax: 20_000 }), h.deps);
  expect(h.scanned).toEqual([[0, 500]]);
  expect(h.published).toHaveLength(1);
  expect(h.published[0].done).toBe(true);
  expect(h.published[0].result.items).toHaveLength(500);
});

test("a stream flush appends instead of rescanning from zero", () => {
  const h = harness();
  const entries = corpus(30_000);
  const ranked = entries.slice(0, 25_000).map(hit);
  startScanJob(spec({ entries, from: 25_000, ranked, immediateMax: 20_000 }), h.deps);
  // 5k pending is under immediateMax, so it runs now and only scores the tail
  expect(h.scanned).toEqual([[25_000, 30_000]]);
  expect(h.published[0].result.items).toHaveLength(30_000);
});

test("cancelling stops the scan before the next slice and publishes nothing more", () => {
  const h = harness();
  const cancel = startScanJob(spec({ entries: corpus(100_000) }), h.deps);
  h.tick();
  const scannedSoFar = h.scanned.length;
  const publishedSoFar = h.published.length;
  cancel();
  h.drain();
  expect(h.scanned).toHaveLength(scannedSoFar);
  expect(h.published).toHaveLength(publishedSoFar);
  expect(h.timers.size).toBe(0);
});

test("results for query A are never published under query B", () => {
  // The search-hold invariant, at its source: the tag travels WITH the data,
  // so a scan started for "alpha" can only ever publish as "alpha".
  const h = harness();
  const cancel = startScanJob(spec({ q: "alpha", entries: corpus(100_000) }), h.deps);
  h.tick();
  cancel(); // the user typed "alphab"
  startScanJob(spec({ q: "alphab", entries: corpus(40_000), sliceSize: 20_000 }), h.deps);
  h.tick();
  h.drain();
  for (const p of h.published) {
    // every published set is internally consistent with its own tag
    expect(p.result.q === "alpha" || p.result.q === "alphab").toBe(true);
  }
  // ...and the final answer belongs to the query the user actually typed
  expect(last(h.published).result.q).toBe("alphab");
  expect(last(h.published).done).toBe(true);
});

test("intermediate publishes are throttled to commitMs", () => {
  const h = harness();
  startScanJob(spec({ entries: corpus(100_000), sliceSize: 20_000, commitMs: 280 }), h.deps);
  h.tick(); // slice 1 — clock still 0, lastPublish 0, so no intermediate publish
  expect(h.published).toHaveLength(0);
  h.tick(300); // slice 2, now past the commit window
  expect(h.published).toHaveLength(1);
  expect(h.published[0].done).toBe(false);
  h.tick(); // slice 3 — inside the window again
  expect(h.published).toHaveLength(1);
});

test("finding nothing yet is not the same as being finished", () => {
  // The two signals are independent, and conflating them is how a scan still
  // running rendered a confident "No matches": a slice that matched nothing
  // publishes an empty list, and only `done` says whether more is coming.
  const h = harness({ score: () => [] });
  startScanJob(spec({ entries: corpus(100_000), sliceSize: 20_000, commitMs: 0 }), h.deps);
  h.tick(); // first slice: nothing matched, four slices still to go
  expect(h.published).toHaveLength(1);
  expect(h.published[0].result.items).toEqual([]);
  expect(h.published[0].done).toBe(false);
  h.drain();
  expect(last(h.published).done).toBe(true);
  expect(last(h.published).result.items).toEqual([]);
});

test("the consumer clears its pending state only on a FINAL publish", () => {
  // Source guard: the flag exists on the publish, and dropping it on the way
  // into state is exactly the regression (the hook used to record only
  // {q, items}, so any publish read as settled).
  const hook = readFileSync(join(import.meta.dir, "useWalkSearch.ts"), "utf8");
  expect(hook).toMatch(/onPublish:\s*\(result,\s*done\)\s*=>\s*setScanned\(\{\s*\.\.\.result,\s*done\s*\}\)/);
  expect(hook).toMatch(/scanPending\s*=\s*searching\s*&&\s*\(scanned\.q\s*!==\s*q\s*\|\|\s*!scanned\.done\)/);
});

test("progress is reported per slice so a cancelled scan can resume", () => {
  const h = harness();
  startScanJob(spec({ entries: corpus(60_000), sliceSize: 20_000 }), h.deps);
  h.tick();
  expect(h.progress).toEqual([20_000]);
  h.drain();
  expect(h.progress).toEqual([20_000, 40_000, 60_000]);
});

test("a job with nothing pending still publishes, tagged for its query", () => {
  // The walk settled without new entries: the caller needs the existing
  // ranking re-tagged for the current query rather than an empty list.
  const h = harness();
  const entries = corpus(10);
  const ranked = entries.map(hit);
  startScanJob(spec({ q: "z", entries, from: 10, ranked }), h.deps);
  expect(h.scanned).toEqual([]);
  expect(h.published).toHaveLength(1);
  expect(h.published[0]).toMatchObject({ done: true });
  expect(h.published[0].result.q).toBe("z");
  expect(h.published[0].result.items).toHaveLength(10);
});
