import { describe, expect, test } from "bun:test";
import { hitsFromRank } from "@apps/explorer/listing/ranked-hits";
import type { IndexRankHit } from "@platform/lib/api";

const hit = (over: Partial<IndexRankHit> = {}): IndexRankHit => ({
  rel: "src/readme.md",
  is_dir: false,
  size: 12,
  mtime: 100,
  ...over,
});

describe("hitsFromRank", () => {
  test("the server's order is kept verbatim", () => {
    const rows = hitsFromRank(
      [hit({ rel: "b.md" }), hit({ rel: "a.md" }), hit({ rel: "c.md" })],
      "md",
    );
    expect(rows.map((r) => r.entry.rel)).toEqual(["b.md", "a.md", "c.md"]);
  });

  test("highlights are re-matched here, not taken off the wire", () => {
    // fuzzy.ts is the single source of truth for what highlights, and the
    // server's ranker is a port of it — so this reproduces the alignment that
    // produced the score rather than trusting a second spelling of it.
    const [row] = hitsFromRank([hit({ rel: "readme.md" })], "rme");
    expect(row.positions.length).toBeGreaterThan(0);
    expect(row.positions.every((p) => p >= 0 && p < "readme.md".length)).toBe(true);
  });

  test("a row the browser's matcher refuses still renders, unhighlighted", () => {
    // The two rankers agree (a parity fixture pins it), but a disagreement
    // must drop the HIGHLIGHT, never the row: a hit the server ranked and the
    // client hid would be a file that exists and cannot be found.
    const [row] = hitsFromRank([hit({ rel: "readme.md" })], "zzz");
    expect(row.entry.rel).toBe("readme.md");
    expect(row.positions).toEqual([]);
  });

  test("the entry is the walk's shape, so the rows downstream are one type", () => {
    const [row] = hitsFromRank([hit({ rel: "d", is_dir: true, size: null })], "d");
    expect(row.entry).toEqual({ rel: "d", is_dir: true, size: null, mtime: 100 });
  });

  test("the ranking fields are placeholders, not read off the wire", () => {
    // `score`/`tier`/`depth`/`longest_run` are no longer on `IndexRankHit` —
    // nothing re-sorts a server-answered row, so the server stops returning
    // them (see api.ts, ranked-hits.ts module comments). `SearchHit` still
    // declares the fields for the live-walk path's `rankCompare`, so this
    // pins the placeholders `hitsFromRank` fills them with, and that they
    // don't vary per hit.
    const [row] = hitsFromRank([hit({ rel: "readable.md" })], "read");
    expect(row.longestRun).toBe("read".length);
    expect(row.tier).toBe(1);
    expect(row.depth).toBe(0);
    expect(row.score).toBe(0);
  });

  test("an empty query has no hits to convert", () => {
    expect(hitsFromRank([hit()], "")).toEqual([]);
  });
});
