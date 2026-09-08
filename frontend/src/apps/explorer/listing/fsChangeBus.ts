// The one thing missing from `window._fusedFsChanged` (main.tsx): it forgets
// what the 5-second prefetch cache knows (api.ts's `clearListPrefetch`), but
// it never told an ALREADY-MOUNTED `useDirListing` that the folder it is
// showing might be wrong. A write from inside a preview iframe — most
// pointedly the git template's stage/unstage running through `fused.runPython`
// — left an open Explorer pane showing exactly what it had before the write,
// because nothing bumped that pane's `refresh` counter. The dir-watch
// WebSocket (useDirListing.ts) has no opinion either: `git add` / `git
// restore --staged` only rewrite bytes inside the existing `.git/index` file,
// so no watched directory's mtime moves and the poller's baseline never
// budges (fused_render/server/watch.py).
//
// A small subscriber list, not a DOM CustomEvent: every mounted listing wants
// the SAME "something changed, re-check" nudge `useDirListing` already gives
// its own WebSocket messages, and a plain callback set is enough to reach
// every one of them without going through the DOM at all.
const subscribers = new Set<() => void>();

/** Register for "a filesystem write happened somewhere, of unknown shape".
 * Returns the unsubscribe function — call it on unmount so an unmounted
 * listing's `setRefresh` is never reached (no leak, no
 * setState-after-unmount). */
export function subscribeFsChanged(cb: () => void): () => void {
  subscribers.add(cb);
  return () => subscribers.delete(cb);
}

/** Tell every subscribed listing to re-check. Called from `window._fusedFsChanged`
 * (main.tsx) alongside `clearListPrefetch`, in addition to it — this reaches
 * listings already mounted; the prefetch cache only protects a listing that
 * mounts LATER. */
export function notifyFsChanged(): void {
  for (const cb of subscribers) cb();
}
