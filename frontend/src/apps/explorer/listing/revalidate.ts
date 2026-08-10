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
// keeps rendering the generation it already has, undimmed, and reconciles at a
// boundary where a repaint costs the user nothing: the query changes, or the
// search ends. Nothing is scored across generations — deferring means keeping
// the old generation's completed results wholesale, never mixing new entries
// into them.
//
// The exception is a change THIS APP made. A user who renames a file and does
// not see the new name has been shown something false, and "don't bother
// updating stale results" was never meant to cover that. In-app mutations are
// already tracked for the index corpus (lib/index-freshness), so the same
// signal reconciles the walk immediately.

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
 * Boundaries the caller drives directly (a query change, a focus) do not go
 * through here — they reconcile unconditionally, because the results are being
 * replaced at that moment anyway.
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
