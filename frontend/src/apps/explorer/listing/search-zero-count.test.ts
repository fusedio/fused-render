// A settled search with zero hits is still a settled search: it renders its
// count (and, once nothing has folded a caveat in, its latency) the same way
// a one-hit search does. No DOM in this suite (same text-parsing pattern as
// search-clear-button.test.ts and search-mode-chip.test.ts): read
// Listing.tsx as text, and exercise the extracted pure pieces directly where
// they exist.
import { expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { IDLE_SEARCH, type SearchState } from "@apps/explorer/listing/types";
import { showingSearchHits } from "@apps/explorer/listing/search-body-mode";

const LISTING = readFileSync(join(import.meta.dir, "../Listing.tsx"), "utf8");

test("the count-chip guard settles on status alone — no hit-count clause survives", () => {
  const at = LISTING.indexOf("if (showsSearchHits && searchState.status === \"ok\") {");
  expect(at).toBeGreaterThan(-1);
  // This is the exact clause that withheld the chip at zero hits; asserting
  // its absence here (rather than just the positive match above) is what
  // fails against the pre-change source, where the guard read
  // `hits.length > 0` on top of the two conditions above.
  expect(LISTING).not.toMatch(/showsSearchHits\s*&&\s*searchState\.status\s*===\s*"ok"\s*&&\s*hits\.length\s*>\s*0/);
});

test("a pending or errored search still cannot set the count — status must read \"ok\"", () => {
  // Both the base count assignment and the latency branch below it require
  // `searchState.status === "ok"` on their own; neither ever lets a pending
  // or errored state through on the strength of `showsSearchHits` alone.
  const guardAt = LISTING.indexOf("if (showsSearchHits && searchState.status === \"ok\") {");
  expect(guardAt).toBeGreaterThan(-1);
  const latencyAt = LISTING.indexOf(
    'else if (searchState.status === "ok" && searchCount !== null) {',
  );
  expect(latencyAt).toBeGreaterThan(guardAt);
});

test("the scan caveat still takes precedence over the latency figure", () => {
  // `if (caveat) { ... } else if (status === "ok" ...)` — the caveat branch
  // must come first and the latency branch must be its `else if`, not a
  // sibling `if`, so a stale count is never paired with a fresh elapsed time.
  const caveatAt = LISTING.indexOf("if (caveat) {");
  const latencyAt = LISTING.indexOf(
    'else if (searchState.status === "ok" && searchCount !== null) {',
  );
  expect(caveatAt).toBeGreaterThan(-1);
  expect(latencyAt).toBeGreaterThan(caveatAt);
});

test("zero hits still take the plural branch, not a bespoke zero case", () => {
  // `match${hits.length === 1 ? "" : "es"}` is 0-safe as written (0 !== 1),
  // so no new zero special case belongs here — asserting the ternary's shape
  // survives unchanged is what would catch someone inventing one.
  expect(LISTING).toMatch(/match\$\{hits\.length === 1 \? "" : "es"\}/);
});

// ITEM 6 (running-screen review, 2026-09-10): the footer used to key on
// raw `searching`, which is true the instant a query is GATED — typed but
// awaiting Enter because it escapes the folder open on screen — showing
// "0 matches" over a folder search never actually ran against. Reuses
// `showsSearchHits` (the SAME notion the body already uses to choose
// search-hit rows over the folder's own, `showingSearchHits` above) rather
// than a second, parallel predicate.
test("the footer's own search/folder switch is showsSearchHits itself, not a second gate", () => {
  const at = LISTING.indexOf("const showsSearchFooter = showsSearchHits;");
  expect(at).toBeGreaterThan(-1);
  // Guards against regressing to the old, narrower gate this fix replaced.
  expect(LISTING).not.toMatch(/showsSearchFooter\s*=\s*searching\s*&&\s*!isPathQuery/);
});

test("the open-folder query still shows no chip: showsSearchHits stays false while uncommitted", () => {
  // A field holding the open folder's own path is an uncommitted query the
  // same way any other escaping query is (decision 4's Enter gate) — the
  // search never actually runs, so `searchState` stays idle and
  // `showingSearchHits` — the one gate the count chip is behind — reads
  // false regardless of `awaitingCommit`.
  expect(showingSearchHits(IDLE_SEARCH, false)).toBe(false);
  expect(showingSearchHits(IDLE_SEARCH, true)).toBe(false);
  const ok: SearchState = {
    status: "ok",
    elapsedMs: 12,
    truncated: false,
    total: 0,
    forRefresh: 0,
  };
  expect(showingSearchHits(ok, true)).toBe(false);
});
