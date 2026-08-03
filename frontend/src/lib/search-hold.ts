// Stale-while-revalidate for the listing's search results (Listing.tsx B3).
//
// A dir-watch event invalidates the walk, which collapses the ranked hits to
// nothing, which would blank the whole result list to "Searching…" while the
// tree re-walks. So the last non-empty answer is HELD and kept on screen
// (dimmed) until a fresh one is ready.
//
// The one thing that must never happen is the previous QUERY's matches showing
// under a new query, so every set of rows is tagged with the query it was
// computed for and a tag mismatch reads as "no rows". Crucially the tag has to
// travel WITH the data, committed at the same moment: tagging at render time is
// wrong, because on the first render after the query changes the committed
// ranking is still the PREVIOUS query's rows (the commit effect has not run
// yet), and a render-time stamp would label those old rows with the new query —
// which is precisely the failure this guard exists to prevent.
//
// Pure and generic so the decision is unit-testable without a DOM
// (search-hold.test.ts).

export interface QueryTagged<T> {
  q: string;
  items: T[];
}

// The rows of a tagged set, or none when it belongs to a different query.
function forQuery<T>(tagged: QueryTagged<T> | null, query: string): T[] {
  return tagged !== null && tagged.q === query ? tagged.items : [];
}

// What to retain as the held answer after this render. Only a non-empty ranking
// for the CURRENT query replaces it; anything else keeps whatever was already
// held (a refresh invalidation empties the ranking without invalidating the
// answer the user is looking at). Leaving search drops the hold entirely.
export function nextHeldHits<T>(
  searching: boolean,
  query: string,
  committed: QueryTagged<T> | null,
  held: QueryTagged<T> | null,
): QueryTagged<T> | null {
  if (!searching) return null;
  const fresh = forQuery(committed, query);
  return fresh.length ? { q: query, items: fresh } : held;
}

// Which rows to render, and whether they are held (pre-refresh) rows standing in
// for a walk that is still running — the caller dims those and keeps the spinner
// up. Held rows are used only while the walk is UNSETTLED: a completed walk with
// no hits is a real "no matches" answer and must replace them, not be papered
// over forever.
export function resolveDisplayedHits<T>(
  searching: boolean,
  query: string,
  committed: QueryTagged<T> | null,
  held: QueryTagged<T> | null,
  walkUnsettled: boolean,
): { hits: T[]; showingHeld: boolean } {
  const fresh = forQuery(committed, query);
  if (fresh.length) return { hits: fresh, showingHeld: false };
  const standIn = searching && walkUnsettled ? forQuery(held, query) : [];
  return standIn.length
    ? { hits: standIn, showingHeld: true }
    : { hits: fresh, showingHeld: false };
}
