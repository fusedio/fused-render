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

// How long a rescan the app asked for may go unaccounted for before the box
// stops claiming one is running.
//
// The claim has to be able to expire, because the server refuses a rescan it
// must not run — a mount, "/", a tree the ignore list excludes, another
// filesystem (server/index_touch.py) — and it does not report that back: the
// client asks for nothing, it is told what happened to a file. Without a
// deadline, mutating a file on a mounted bucket leaves "indexing…" on screen
// for the rest of the session, and suppresses the `behind` caveat that is the
// TRUE one there. Generous, because it is only wrong in one direction: a
// coalesce, a scan and a status poll all have to fit inside it.
export const RESCAN_PENDING_MAX_MS = 60_000;

// When the app last changed something that no completed scan has covered yet,
// or null. Set by any in-app change, cleared by the next index lifecycle event
// — which is precisely "a scan finished" (lib/index-status observes it).
let mutatedAt: number | null = null;

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
 * True from the moment of the change until a scan completes — or until the
 * deadline above, for the rescans the server refuses and never reports. Not
 * folder-scoped and deliberately so: the server decides which folder its rescan covers, and
 * a second opinion here — kept in a different vocabulary, on a different
 * clock — is exactly the kind of duplicated policy the ranked search removed.
 * What the client needs from it is one bit: say "indexing…" or don't.
 */
export function indexRescanPending(now: number = Date.now()): boolean {
  return mutatedAt !== null && now - mutatedAt < RESCAN_PENDING_MAX_MS;
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
  mutatedAt = null;
  for (const fn of lifecycleListeners) fn();
}

/**
 * Record that this app changed `path` (created, deleted, renamed, written).
 *
 * `rescans` says whether the SERVER will re-index that folder for this change,
 * and the two signals part company there. The count moves either way — the
 * listing owes the user their own edit immediately, whatever the index does
 * (listing/revalidate) — while the "indexing…" claim is only made for a change
 * the server actually acts on. Overwriting a file is the case: it changes
 * bytes, not names, so the server deliberately schedules nothing
 * (server/index_touch.py), and claiming a rescan for it left the caption up
 * for a minute AND suppressed the "not refreshed" caveat that was true.
 */
export function noteFsMutation(
  path: string,
  opts: { rescans?: boolean; now?: number } = {},
): void {
  const p = String(path || "").replace(/\/+$/, "");
  if (!p || p === "/") return;
  if (opts.rescans !== false) mutatedAt = opts.now ?? Date.now();
  mutations++;
  for (const fn of listeners) fn();
}

/** Tests only. */
export function resetFsMutations(): void {
  mutations = 0;
  lifecycle = 0;
  mutatedAt = null;
}
