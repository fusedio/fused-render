// Who owns the explorer bar's LAYOUT ZONE right now — the crumb bar, or the
// folder listing underneath it.
//
// Over a FOLDER the zone's contents are wrong in two different ways. The split
// buttons offer to tear the window in half at the moment the view is already a
// split (list + preview pane), which is the one place a second one buys
// nothing; and the `···` sits at the far end of a bar whose other end is the
// path, three hundred pixels from the search box that is the folder's actual
// control. So a folder view CLAIMS the zone: the splits go away and the `···`
// re-renders inside the listing's search row, next to the thing it acts on
// (Listing.tsx). A file/preview keeps the zone exactly as it was.
//
// A module store rather than a prop because the two components are SIBLINGS —
// App renders <Breadcrumb> above <Listing>/<Preview>, and which of those the
// content resolves to is decided several levels down (a directory's `_mode`
// can leave the listing for `git`/`history` without remounting the bar). The
// same shape as lib/ui-overlay and fs-clipboard: state that lives just outside
// a remount boundary, with a subscribe/snapshot pair for useSyncExternalStore.
//
// Counted, not a boolean: a claim is released on unmount, and React mounts the
// incoming view before unmounting the outgoing one, so a straight `false` on
// release would leave the zone hidden after a folder→folder hop or shown after
// a scaffold swap. Claims are idempotent per caller (each returns its own
// release, called at most once by the effect that made it).
let claims = 0;
const subscribers = new Set<() => void>();

function notify(): void {
  for (const fn of subscribers) fn();
}

export function claimFolderChrome(): () => void {
  claims += 1;
  notify();
  let released = false;
  return () => {
    if (released) return;
    released = true;
    claims -= 1;
    notify();
  };
}

export function folderChromeClaimed(): boolean {
  return claims > 0;
}

export function subscribeFolderChrome(fn: () => void): () => void {
  subscribers.add(fn);
  return () => {
    subscribers.delete(fn);
  };
}

// Test seam: the store outlives any component, so a test that claims must be
// able to put it back.
export function resetFolderChrome(): void {
  claims = 0;
  notify();
}
