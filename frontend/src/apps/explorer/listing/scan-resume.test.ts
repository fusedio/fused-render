import { describe, expect, it } from "bun:test";
import { canResumeScan, type ScanCache } from "@apps/explorer/listing/scan-resume";
import type { WalkEntry } from "@platform/lib/api";

function entries(n: number): WalkEntry[] {
  return Array.from({ length: n }, (_, i) => ({
    rel: `f${i}.txt`,
    is_dir: false,
    size: 1,
    mtime: 2,
  }));
}

const A = entries(100);

function cache(over: Partial<ScanCache> = {}): ScanCache {
  return { q: "rep", showHidden: false, entries: A, key: "k1", scored: 100, ...over };
}

describe("canResumeScan", () => {
  it("resumes a streaming walk — one array, appended in place", () => {
    expect(canResumeScan(cache(), { q: "rep", showHidden: false, entries: A, key: "k1" })).toBe(true);
  });

  it("resumes a REFETCH of the same corpus, which is a different array", () => {
    // The bug: an in-app rename or an error retry re-runs the fetch, and the
    // whole 200k-entry corpus was re-scored because the array was new.
    const refetched = entries(100);
    expect(canResumeScan(cache(), { q: "rep", showHidden: false, entries: refetched, key: "k1" })).toBe(
      true,
    );
  });

  it("re-scores from zero when the corpus content changed", () => {
    expect(
      canResumeScan(cache(), { q: "rep", showHidden: false, entries: entries(120), key: "k2" }),
    ).toBe(false);
  });

  it("re-scores from zero when the corpus has no identity to claim", () => {
    expect(
      canResumeScan(cache({ key: "" }), { q: "rep", showHidden: false, entries: entries(100), key: "" }),
    ).toBe(false);
  });

  it("re-scores when the new array is shorter than the progress mark", () => {
    // A retry rebuilds the array from empty; resuming at 100 would skip every
    // row it has streamed so far.
    expect(
      canResumeScan(cache(), { q: "rep", showHidden: false, entries: entries(20), key: "k1" }),
    ).toBe(false);
  });

  it("never resumes across a query or hidden-intent change", () => {
    expect(canResumeScan(cache(), { q: "repo", showHidden: false, entries: A, key: "k1" })).toBe(false);
    expect(canResumeScan(cache(), { q: "rep", showHidden: true, entries: A, key: "k1" })).toBe(false);
  });
});
