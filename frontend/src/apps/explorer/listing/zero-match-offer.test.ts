// The reserved row-path both row-activation call sites (Listing.tsx's
// onRowPointerUp, useListingSelection.ts's Enter case) branch on to run the
// zero-match glob-broadening offer instead of navigate() — and the
// derivation of whether that offer should exist at all.
import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import {
  isZeroMatchOfferPath,
  settledZeroMatchOffer,
  ZERO_MATCH_OFFER_PATH,
} from "@apps/explorer/listing/zero-match-offer";
import type { SearchState } from "@apps/explorer/listing/types";

describe("the zero-match offer sentinel", () => {
  test("carries a NUL byte, which is illegal in a POSIX or Windows path — no real row can ever collide with it", () => {
    expect(ZERO_MATCH_OFFER_PATH).toContain("\0");
  });

  test("the predicate recognizes only the sentinel itself", () => {
    expect(isZeroMatchOfferPath(ZERO_MATCH_OFFER_PATH)).toBe(true);
  });

  test("the predicate rejects real filesystem paths, including ones that echo the sentinel's own words", () => {
    expect(isZeroMatchOfferPath("/home/iamsdas/zero-match-offer")).toBe(false);
    expect(isZeroMatchOfferPath("")).toBe(false);
    expect(isZeroMatchOfferPath("/")).toBe(false);
  });
});

// Both wiring bugs below are pinned at the source: neither the sentinel
// branch of onRowPointerUp nor the offer row's own <button> can be driven
// through a headless React renderer (Listing.tsx has no full-mount test
// harness — see selection.test.ts's own header and its "the listing rows
// wire both halves of the model" precedent for this same technique).
describe("the offer row's activation wiring", () => {
  const src = readFileSync(join(import.meta.dir, "../Listing.tsx"), "utf8");

  test("the sentinel branch only reruns on the primary button, like every real row's press does", () => {
    const branch = src.slice(
      src.indexOf("if (isZeroMatchOfferPath(path)) {"),
      src.indexOf("const press = pressRef.current;"),
    );
    expect(branch).toMatch(/if \(e\.button !== 0\) return;/);
  });

  test("the offer button wires onClick, so a keyboard Tab+Enter/Space can activate it", () => {
    // `navActive` (useListingSelection.ts) requires focus on the search
    // input or document body/root — a focused <button> fails that check, so
    // the document-level Enter handler never reaches this offer at all
    // unless the button answers a plain `click` itself.
    const row = src.slice(
      src.indexOf("broadenOffer !== null ? ("),
      src.indexOf(") : ("),
    );
    expect(row).toContain("onClick={");
    // Guarded against double-firing for a real pointer interaction, which
    // onPointerUp above already handles — see the guard's own comment.
    expect(row).toMatch(/if \(e\.detail !== 0\) return;/);
  });
});

// settledZeroMatchOffer's contract: the glob-broaden ladder (glob-broaden.ts)
// only ever gets asked for a query that has ACTUALLY come back with zero
// hits — never one the box has moved on to while an older, unrelated answer
// is still on screen.
//
// Code review finding (Cursor Bugbot, 2026-09-10): `mode`, `reason` and
// `displayHits` are all read off the same `answer` object, and that object
// only changes when a new one lands — so they move in lockstep with EACH
// OTHER. They do NOT move in lockstep with `q`: `q` (`useDeferredValue`)
// catches up to a keystroke well before the fetch effect's own trailing
// debounce fires the request that would produce a new `answer` for it. In
// that window — box already showing the next query's text, but no request
// for it even out yet — `scanPending` (`pending || polling`) is still
// false, because `pending` is not set until the debounce elapses and `run()`
// actually issues the call. A "settled" check built from
// `!scanPending && searchState.status !== "pending"` alone reads that window
// as settled, and licenses a broaden offer for a query that was never
// checked for zero hits at all.
//
// `rowsAnswerQuery` (useListingSearch.ts) already exists to answer exactly
// this: it compares the answer actually on screen against `q` directly
// (`staleRows`), so it goes false the instant `q` moves past what `answer`
// is for — no request needs to have gone out yet. Requiring it here is the
// fix: one flag that says which query the rows/mode/reason on screen answer,
// instead of trusting `scanPending` and `searchState` to have already caught
// up.
const OK: SearchState = { status: "ok", truncated: false, total: 0, forRefresh: 0, elapsedMs: 1 };
const PENDING: SearchState = { status: "pending", forRefresh: 0 };
const ERROR: SearchState = { status: "error", message: "boom", forRefresh: 0 };

function base() {
  return {
    showsSearchHits: true,
    rowsAnswerQuery: true,
    searchState: OK,
    scanPending: false,
    displayHitsLength: 0,
    mode: "glob" as const,
    reason: "" as const,
    q: "*.txt",
  };
}

describe("settledZeroMatchOffer", () => {
  test("offers to broaden a genuinely settled, zero-hit glob search", () => {
    expect(settledZeroMatchOffer(base())).toEqual({ pattern: "*.txt*", label: "Widen the name" });
  });

  // The finding itself: `scanPending` is false and `searchState` is still
  // "ok" during the debounce wait before a NEW request for the edited query
  // has even been scheduled to fire — the on-screen `displayHits`/`mode`/
  // `reason` still describe the PREVIOUS query's answer. `rowsAnswerQuery`
  // is the one signal that already knows the rows on screen do not answer
  // `q` any more, and must gate the offer.
  test("no offer while rowsAnswerQuery says the rows on screen answer an older query", () => {
    expect(settledZeroMatchOffer({ ...base(), rowsAnswerQuery: false })).toBeNull();
  });

  test("no offer while a request is genuinely in flight (scanPending)", () => {
    expect(settledZeroMatchOffer({ ...base(), scanPending: true })).toBeNull();
  });

  test("no offer while the search itself is pending", () => {
    expect(settledZeroMatchOffer({ ...base(), searchState: PENDING })).toBeNull();
  });

  test("no offer on a settled error — that's EmptyResultMessage's job, not a broaden offer", () => {
    expect(settledZeroMatchOffer({ ...base(), searchState: ERROR })).toBeNull();
  });

  test("no offer once there are hits on screen", () => {
    expect(settledZeroMatchOffer({ ...base(), displayHitsLength: 3 })).toBeNull();
  });

  test("no offer for a substring search — broadening is a PATTERN concept", () => {
    expect(settledZeroMatchOffer({ ...base(), mode: "substring" })).toBeNull();
  });

  test("no offer for a non-empty reason — an index gap, not a genuine zero-hit answer", () => {
    expect(settledZeroMatchOffer({ ...base(), reason: "uncovered" })).toBeNull();
  });

  test("no offer when the folder's own rows are on screen, not search hits", () => {
    expect(settledZeroMatchOffer({ ...base(), showsSearchHits: false })).toBeNull();
  });

  test("null when the ladder itself has nothing left to widen", () => {
    expect(settledZeroMatchOffer({ ...base(), q: "**/*.txt*" })).toBeNull();
  });
});
