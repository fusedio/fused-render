// Two signals about the file index that nothing else in the app reports: this
// app changed something on disk, and the index itself moved.
//
// There is no filesystem watcher (index/specs/scan.md), so the index is a
// snapshot: rename a file and it keeps offering the old name until something
// scans that folder again. For out-of-band edits that is the documented trade.
// For an edit the explorer itself just made it was a visible lie, and this
// module used to answer it by DISQUALIFYING the index: `indexMayAnswer` marked
// the mutated folder (plus its ancestors and its descendants, since in-folder
// search is recursive) and the search walked it live for the rest of the
// session.
//
// That gate is gone, because the walk it escaped to is gone. The server now
// rescans the folder the app changed (server/index_touch.py), so the honest
// state is not "do not trust this folder" but "the fix is on its way" —
// `indexRescanPending`, which the search box turns into the same "indexing…"
// caption a scan started anywhere else produces.
//
// Both counters are session-scoped and in-memory on purpose. A reload gets a
// fresh startup scan, which is what actually repairs the index.

// Monotonic count of mutations, and the listeners watching for them. The
// search's revalidation deferral (listing/revalidate) reads this: a change the
// USER just made is the one case that must repaint immediately, so it needs a
// signal rather than a poll.
let mutations = 0;
const listeners = new Set<() => void>();

// Whether a mutation has been made that no completed scan has covered yet.
// Set by any in-app change, cleared by the next index lifecycle event — which
// is precisely "a scan finished" (lib/index-status observes it).
let rescanPending = false;

/** How many mutations this app has made this session. */
export function fsMutationCount(): number {
  return mutations;
}

/** Subscribe to in-app mutations. Returns an unsubscribe function. */
export function subscribeFsMutations(fn: () => void): () => void {
  listeners.add(fn);
  return () => void listeners.delete(fn);
}

/**
 * Whether the index is still catching up with a change this app made.
 *
 * True from the moment of the change until a scan completes. Not folder-scoped
 * and deliberately so: the server decides which folder its rescan covers, and
 * a second opinion here — kept in a different vocabulary, on a different
 * clock — is exactly the kind of duplicated policy the ranked search removed.
 * What the client needs from it is one bit: say "indexing…" or don't.
 */
export function indexRescanPending(): boolean {
  return rescanPending;
}

// The index's own lifecycle: deleted from Preferences, or a scan completing.
// Distinct from `mutations` because it carries no dirty folder — a rebuilt
// index is MORE trustworthy, not less — but an answer fetched before it is a
// generation behind either way, and nothing else moves the fetch key: the
// filesystem didn't change, so no dir-watch refresh ever comes.
let lifecycle = 0;
const lifecycleListeners = new Set<() => void>();

/** How many times the index itself was deleted/rebuilt this session. */
export function indexLifecycleCount(): number {
  return lifecycle;
}

/** Subscribe to index lifecycle events. Returns an unsubscribe function. */
export function subscribeIndexLifecycle(fn: () => void): () => void {
  lifecycleListeners.add(fn);
  return () => void lifecycleListeners.delete(fn);
}

/** Record that the index was deleted or a scan finished. */
export function noteIndexLifecycle(): void {
  lifecycle++;
  rescanPending = false;
  for (const fn of lifecycleListeners) fn();
}

/** Record that this app changed `path` (created, deleted, renamed, written). */
export function noteFsMutation(path: string): void {
  const p = String(path || "").replace(/\/+$/, "");
  if (!p || p === "/") return;
  rescanPending = true;
  mutations++;
  for (const fn of listeners) fn();
}

/** Tests only. */
export function resetFsMutations(): void {
  mutations = 0;
  lifecycle = 0;
  rescanPending = false;
}
