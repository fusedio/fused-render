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
    // fuzzy.ts (`substringMatch`) is the single source of truth for what
    // highlights — this reproduces the alignment rather than trusting a
    // second spelling of it. A genuine substring, not merely a subsequence:
    // every row here already passed the server's substring filter, and
    // `hitsFromRank` matches with the same test (`substringMatch`, not the
    // looser `fuzzyMatch`) so the guarantee is explicit rather than
    // incidental.
    const [row] = hitsFromRank([hit({ rel: "readme.md" })], "eadm");
    expect(row.positions).toEqual([1, 2, 3, 4]);
  });

  test("a row the browser's matcher refuses still renders, unhighlighted", () => {
    // Client and server are expected to agree on every substring hit; a
    // disagreement (a real one, or a manufactured one like this) must drop
    // the HIGHLIGHT, never the row: a hit the server ranked and the client
    // hid would be a file that exists and cannot be found.
    const [row] = hitsFromRank([hit({ rel: "readme.md" })], "zzz");
    expect(row.entry.rel).toBe("readme.md");
    expect(row.positions).toEqual([]);
  });

  test("the entry is the WalkEntry shape, so the rows downstream are one type", () => {
    const [row] = hitsFromRank([hit({ rel: "d", is_dir: true, size: null })], "d");
    expect(row.entry).toEqual({ rel: "d", is_dir: true, size: null, mtime: 100 });
  });

  test("an empty query has no hits to convert", () => {
    expect(hitsFromRank([hit()], "")).toEqual([]);
  });

  test("a substring-mode hit still highlights, unchanged", () => {
    const [row] = hitsFromRank([hit({ rel: "readme.md" })], "eadm", "substring");
    expect(row.positions).toEqual([1, 2, 3, 4]);
  });

  test("a glob-mode hit is never re-tested with substringMatch, and always kept", () => {
    // A glob hit is not necessarily a substring of the query text at all —
    // `*.csv` matching `report.csv` has no literal `"*.csv"` anywhere in the
    // path. Re-running substringMatch over it would either mislabel a
    // coincidental substring as the match or, for a hit with none, produce
    // the same `[]` as a hit that should have been highlighted — both
    // indistinguishable from a bug without consulting the server's own mode.
    const [row] = hitsFromRank([hit({ rel: "report.csv" })], "*.csv", "glob");
    expect(row.entry.rel).toBe("report.csv");
    expect(row.positions).toEqual([]);
  });

  test("a query carrying a base prefix the server already consumed still highlights the leaf", () => {
    // `q` is the query exactly as typed — useListingSearch.ts never strips a
    // base prefix client-side — but `h.rel` is relative to the server's
    // resolved base, not to the query. "~/other/rep" can never be a literal
    // substring of "report.csv"; the segment after the last "/" ("rep") is
    // what the server actually matched, and that's what the highlight has
    // to land on.
    const [row] = hitsFromRank([hit({ rel: "report.csv" })], "~/other/rep");
    expect(row.entry.rel).toBe("report.csv");
    expect(row.positions).toEqual([0, 1, 2]);
  });

  test("mode defaults to substring, so existing callers keep today's behavior", () => {
    const [row] = hitsFromRank([hit({ rel: "readme.md" })], "eadm");
    expect(row.positions).toEqual([1, 2, 3, 4]);
  });
});
