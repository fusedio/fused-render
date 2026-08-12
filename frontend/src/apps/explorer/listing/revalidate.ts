// When an active search is allowed to notice that the tree changed.
//
// The dir watch bumps a refresh generation on every filesystem event under the
// folder. Outside search that is exactly right — the listing repaints. During a
// search it was not: each bump invalidated the corpus, which collapsed the
// ranked hits, which put the held rows on screen dimmed under a spinner while a
// refetch ran. Under a churny root (a home directory: Library/, logs, the index
// worker's own writes) that is a flash of "scanning" every few seconds, while
// the user is trying to read their results.
//
// So a watch bump during an active search is RECORDED, not applied. The search
// keeps rendering the generation it already has and reconciles only where a
// repaint costs the user nothing. Nothing is scored across generations —
// deferring means keeping the old generation's completed results wholesale,
// never mixing new entries into them.
//
// A QUERY CHANGE is no longer one of those places, and that is the whole of
// this file's second version. Treating it as a boundary meant every keystroke
// after any background churn adopted the pending generation, which invalidated
// the corpus, which spent a round trip before the new query could rank
// anything — a blank list on a keystroke, which is worse than any staleness it
// avoided. The index bumps the same counter every time a scan completes
// (lib/index-freshness), and scans complete often, so this was not a rare
// path. Ranking a new query against the corpus in hand is instant; the caller
// dims the rows and captions them, and being a generation behind is a state
// this search can live in. What remains is: the search ending (nothing is on
// screen to protect, and it is what re-arms the next search with fresh data)
// and an in-app mutation.
//
// That mutation exception is not negotiable. A user who renames a file and
// does not see the new name has been shown something false, and "don't bother
// updating stale results" was never meant to cover that. In-app mutations are
// already tracked for the index corpus (lib/index-freshness), so the same
// signal reconciles the walk immediately — and the caller drops its held
// corpus with it, since that corpus holds the pre-rename name.

export interface RevalidateInput {
  /** Latest generation from the dir watch. */
  refresh: number;
  /** Generation the search is currently rendering. */
  pinned: number;
  /** Whether a search is active (an empty query is not). */
  searching: boolean;
  /** Monotonic count of mutations this app has made. */
  mutations: number;
  /** The count as of the last reconcile. */
  appliedMutations: number;
}

/**
 * Whether the pending watch generation should be adopted now.
 *
 * Every boundary goes through here. Nothing the caller does reconciles on its
 * own any more — the one thing that used to (a query change) is precisely the
 * churn described above.
 */
export function shouldReconcile(input: RevalidateInput): boolean {
  // Nothing pending.
  if (input.refresh === input.pinned) return false;
  // Not searching: the plain listing revalidates on every event, as it always
  // has. This also covers the search being cleared — the moment `searching`
  // goes false the deferred generation lands.
  if (!input.searching) return true;
  // This app changed something under here; the user is owed the truth.
  return input.mutations !== input.appliedMutations;
}
