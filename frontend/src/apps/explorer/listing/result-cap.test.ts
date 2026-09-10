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

test("a settled search with no hits still reports a count, pluralised as a plural", () => {
  // Zero is not one, so it takes the "es" branch same as any other count that
  // isn't 1 — no zero-specific case needed here or in the chip that reuses
  // this shape.
  expect(resultCountLabel(0, false)).toBe("0 matches");
});

test("an empty hit list is not reported as capped", () => {
  // cappedAway (useListingSearch.ts) is displayHits.length - visibleHits.length;
  // an empty list caps away nothing, so the "top N of M" branch never fires
  // at zero hits.
  expect(capHits(hits(0))).toHaveLength(0);
});

test("the rank-limit marker survives the cap", () => {
  // A server rank truncation means the count itself undercounts the tree;
  // that "+" has to stay visible whether or not the LIST is also capped.
  expect(resultCountLabel(42, true)).toBe("42+ matches");
  expect(resultCountLabel(4880, true)).toBe("Showing top 100 of 4,880+");
});

test("the cap is confined to the SEARCH path", () => {
  // The plain listing renders whole folders and must keep doing so — a
  // hundred-row cap on a normal directory would be data loss, not restraint.
  // The cap reaches the UI only through `visibleHits`, which Listing.tsx uses
  // exclusively while `searching`; the non-search branch reads sortedEntries.
  const listing = readFileSync(join(import.meta.dir, "../Listing.tsx"), "utf8");
  const hook = readFileSync(join(import.meta.dir, "useListingSearch.ts"), "utf8");
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

test("a glob answer is capped at the top SEARCH_RESULT_CAP, same as a substring answer", () => {
  // A glob's matches are all equally relevant — there is no ranking tail to
  // trim — but an unbounded pattern (e.g. a shallow "*.zip" over a huge tree)
  // can still return thousands of equally-valid hits, and a list that long is
  // exactly the display problem the substring cap already exists to solve.
  // Same cap, same layer, for the same reason.
  const all = hits(4880);
  const shown = capHits(all, "glob");
  expect(shown).toHaveLength(SEARCH_RESULT_CAP);
  expect(shown.map((h) => h.entry.rel)).toEqual(
    all.slice(0, SEARCH_RESULT_CAP).map((h) => h.entry.rel),
  );
});

test("a short glob result list is untouched, same as substring", () => {
  const all = hits(7);
  expect(capHits(all, "glob")).toBe(all); // same array, no copy
});

test("a glob answer's count owns up to the cap exactly like a substring answer's", () => {
  expect(resultCountLabel(4880, false, "glob")).toBe("Showing top 100 of 4,880");
  expect(resultCountLabel(4880, true, "glob")).toBe("Showing top 100 of 4,880+");
  // Under the cap, the plain count — nothing to own up to either way.
  expect(resultCountLabel(42, false, "glob")).toBe("42 matches");
});
