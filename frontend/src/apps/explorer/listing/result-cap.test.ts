// The search results display cap.
//
// Ranking runs over the whole corpus; only the LIST is capped. Past the first
// hundred, a fuzzy rank is not telling the user anything they can use — the
// useful move is a better query, not more scrolling — so the tail is not
// rendered and the counter says so.
import { expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import type { WalkEntry } from "@platform/lib/api";
import { SEARCH_RESULT_CAP } from "@apps/explorer/listing/types";
import { capHits, resultCountLabel } from "@apps/explorer/listing/result-cap";
import type { SearchHit } from "@apps/explorer/listing/types";

function hits(n: number): SearchHit[] {
  const out: SearchHit[] = [];
  for (let i = 0; i < n; i++) {
    out.push({
      entry: { rel: `f${i}.ts`, is_dir: false, size: 1, mtime: 1 } as WalkEntry,
      positions: [],
      // descending score, so index order IS rank order
      score: n - i,
      longestRun: 1,
      depth: 1,
    });
  }
  return out;
}

test("the cap is a hundred", () => {
  expect(SEARCH_RESULT_CAP).toBe(100);
});

test("a short result list is untouched", () => {
  const all = hits(7);
  expect(capHits(all)).toBe(all); // same array, no copy
});

test("a long result list renders only the top of the RANKING", () => {
  const all = hits(4880);
  const shown = capHits(all);
  expect(shown).toHaveLength(SEARCH_RESULT_CAP);
  // the first hundred of the ranked order, in order — not a sample
  expect(shown.map((h) => h.entry.rel)).toEqual(
    all.slice(0, SEARCH_RESULT_CAP).map((h) => h.entry.rel),
  );
});

test("the counter reports the TRUE total, not the capped one", () => {
  // Undercounting would be a lie about the folder; the cap is about the list.
  // No "refine your search": the number is the information, the instruction
  // was noise.
  expect(resultCountLabel(4880, false)).toBe("Showing top 100 of 4,880");
});

test("an uncapped result set keeps the plain count", () => {
  expect(resultCountLabel(1, false)).toBe("1 match");
  expect(resultCountLabel(42, false)).toBe("42 matches");
  expect(resultCountLabel(SEARCH_RESULT_CAP, false)).toBe("100 matches");
});

test("the walk-truncated marker survives the cap", () => {
  // A capped walk means the count itself undercounts the tree; that "+" has
  // to stay visible whether or not the LIST is also capped.
  expect(resultCountLabel(42, true)).toBe("42+ matches");
  expect(resultCountLabel(4880, true)).toBe("Showing top 100 of 4,880+");
});

test("the cap is confined to the SEARCH path", () => {
  // The plain listing renders whole folders and must keep doing so — a
  // hundred-row cap on a normal directory would be data loss, not restraint.
  // The cap reaches the UI only through `visibleHits`, which Listing.tsx uses
  // exclusively while `searching`; the non-search branch reads sortedEntries.
  const listing = readFileSync(join(import.meta.dir, "../Listing.tsx"), "utf8");
  const hook = readFileSync(join(import.meta.dir, "useWalkSearch.ts"), "utf8");
  expect(listing).not.toContain("SEARCH_RESULT_CAP");
  expect(listing).not.toContain("capHits");
  const capLines = hook.split("\n").filter((l) => l.includes("capHits("));
  expect(capLines).toHaveLength(1);
  expect(capLines[0]).toContain("displayHits");
});

test("exactly at the cap is not reported as capped", () => {
  // Nothing is hidden, so there is nothing to refine.
  expect(capHits(hits(SEARCH_RESULT_CAP))).toHaveLength(SEARCH_RESULT_CAP);
  expect(resultCountLabel(SEARCH_RESULT_CAP, false)).toBe("100 matches");
});
