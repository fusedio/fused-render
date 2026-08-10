import { describe, expect, it } from "bun:test";
import { indexCorpusFrom } from "@apps/explorer/listing/index-corpus";
import type { IndexSearchResult } from "@platform/lib/api";

function res(over: Partial<IndexSearchResult> = {}): IndexSearchResult {
  return {
    covered: true,
    fresh: true,
    root: "/r",
    entries: [{ rel: "a.txt", is_dir: false, size: 1, mtime: 2 }],
    truncated: false,
    total: 1,
    updated: 100,
    age_s: 1,
    ...over,
  };
}

describe("indexCorpusFrom", () => {
  it("uses the index when the folder is covered and fresh", () => {
    const corpus = indexCorpusFrom(res());
    expect(corpus?.entries.map((e) => e.rel)).toEqual(["a.txt"]);
    expect(corpus?.truncated).toBe(false);
  });

  it("falls back to the walk when the folder is not covered", () => {
    expect(indexCorpusFrom(res({ covered: false }))).toBeNull();
  });

  it("still answers from a stale index — the UI says so, the walk is not faster", () => {
    expect(indexCorpusFrom(res({ fresh: false }))?.entries).toHaveLength(1);
  });

  it("falls back to the walk when there is no answer at all", () => {
    expect(indexCorpusFrom(null)).toBeNull();
    expect(indexCorpusFrom(undefined)).toBeNull();
  });

  it("falls back when the payload is malformed rather than trusting it", () => {
    expect(indexCorpusFrom(res({ entries: undefined as never }))).toBeNull();
  });

  it("still uses a truncated corpus — the walk caps the same way", () => {
    expect(indexCorpusFrom(res({ truncated: true }))?.truncated).toBe(true);
  });
});
