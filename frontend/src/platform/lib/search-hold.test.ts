// Stale-while-revalidate hold for search results. The regression these guard is
// specific: a dir-watch refresh must keep the previous answer on screen, and a
// QUERY CHANGE must not — the whole point of tagging the rows.
import { expect, test } from "bun:test";

import { nextHeldHits, resolveDisplayedHits, type QueryTagged } from "@platform/lib/search-hold";

const tagged = (q: string, ...items: string[]): QueryTagged<string> => ({ q, items });
const display = (
  query: string,
  committed: QueryTagged<string> | null,
  held: QueryTagged<string> | null,
  walkUnsettled: boolean,
  searching = true,
) => resolveDisplayedHits(searching, query, committed, held, walkUnsettled);

test("a fresh ranking for the current query is what renders", () => {
  const r = display("foo", tagged("foo", "a", "b"), tagged("foo", "old"), false);
  expect(r).toEqual({ hits: ["a", "b"], showingHeld: false });
});

test("a refresh invalidation keeps the previous answer for the SAME query", () => {
  // The walk went idle (refresh generation bumped), so the committed ranking is
  // empty — but the query is unchanged and the tree is being re-walked.
  const r = display("foo", tagged("foo"), tagged("foo", "a", "b"), true);
  expect(r).toEqual({ hits: ["a", "b"], showingHeld: true });
});

test("a NEW query never renders the old query's matches", () => {
  // The exact reported bug: while the new walk is unsettled with no hits, the
  // held rows belong to "foo" and the query on screen is "foobar".
  const r = display("foobar", tagged("foobar"), tagged("foo", "a", "b"), true);
  expect(r).toEqual({ hits: [], showingHeld: false });
});

test("a committed ranking still tagged with the OLD query counts as no rows", () => {
  // This is the render where the bug lived. `q` has already changed but the
  // committed ranking state still holds the previous query's rows, because the
  // commit effect has not run yet. A tag mismatch has to read as empty, or those
  // rows get relabelled with the new query and then held for the whole walk.
  const stale = tagged("foo", "a", "b");
  expect(display("foobar", stale, null, true)).toEqual({ hits: [], showingHeld: false });
  // …and it must not be promoted into the hold either.
  expect(nextHeldHits(true, "foobar", stale, null)).toBeNull();
});

test("a completed walk with no hits replaces the held rows", () => {
  // walkUnsettled false = the walk finished and genuinely found nothing (the
  // file was just deleted). Holding forever would be a lie.
  const r = display("foo", tagged("foo"), tagged("foo", "a", "b"), false);
  expect(r).toEqual({ hits: [], showingHeld: false });
});

test("nothing is held once search is left", () => {
  expect(nextHeldHits(false, "", tagged("", ), tagged("foo", "a"))).toBeNull();
  expect(display("", tagged(""), tagged("foo", "a"), true, false)).toEqual({
    hits: [],
    showingHeld: false,
  });
});

test("the hold advances only on a non-empty ranking for the current query", () => {
  const held = tagged("foo", "a");
  // Empty ranking (mid-refresh): keep what we had.
  expect(nextHeldHits(true, "foo", tagged("foo"), held)).toBe(held);
  // Fresh answer: replace it, tagged with the query it was computed for.
  expect(nextHeldHits(true, "foo", tagged("foo", "b", "c"), held)).toEqual({
    q: "foo",
    items: ["b", "c"],
  });
});

test("held rows survive several consecutive refresh invalidations", () => {
  let held: QueryTagged<string> | null = null;
  held = nextHeldHits(true, "foo", tagged("foo", "a", "b"), held);
  for (let i = 0; i < 3; i++) {
    const r = display("foo", tagged("foo"), held, true);
    expect(r.showingHeld).toBe(true);
    expect(r.hits).toEqual(["a", "b"]);
    held = nextHeldHits(true, "foo", tagged("foo"), held);
  }
});

test("a query change clears the hold's usefulness permanently, not just once", () => {
  // Old hold is never reachable again: a later refresh under the new query must
  // not resurrect it.
  const stale = tagged("foo", "a");
  const held = nextHeldHits(true, "bar", tagged("bar"), stale);
  expect(held).toBe(stale); // still retained (cheap), but…
  expect(display("bar", tagged("bar"), held, true).hits).toEqual([]); // …never shown
});
