// Who owns the explorer's CRUMB BAR right now — the shell, or the folder
// listing underneath it.
//
// Over a FOLDER the bar is wrong in two different ways. Its layout zone offers
// to tear the window in half at the moment the view is already a split (list +
// preview pane), which is the one place a second one buys nothing; and the
// `···` sits at the far end of a bar whose other end is the path, three
// hundred pixels from the search box that is the folder's actual control. So a
// folder view CLAIMS the bar: the splits go away and the `···` re-renders
// inside the listing's search row, next to the thing it acts on (Listing.tsx).
//
// The claim also carries a SLOT — a node inside the listing's left column that
// the whole bar portals into (Breadcrumb.tsx `BreadcrumbBar`). That is what
// makes the preview pane full height: with the bar confined to the left
// column, the pane's own header is the topmost thing on the right and the pane
// runs from the top of the window to the bottom. A file/preview claims
// nothing, and its bar renders at shell level spanning the full width, exactly
// as before.
//
// A module store rather than a prop because the two components are SIBLINGS —
// App renders <Breadcrumb> above <Listing>/<Preview>, and which of those the
// content resolves to is decided several levels down (a directory's `_mode`
// can leave the listing for `git` without remounting the bar). The
// same shape as lib/ui-overlay and fs-clipboard: state that lives just outside
// a remount boundary, with a subscribe/snapshot pair for useSyncExternalStore.
//
// A STACK, not a boolean: a claim is released on unmount, and a swap can hold
// two folder views at once (folder→folder hop, scaffold→resolved), so a
// straight `false` on release would leave the bar hidden or doubly rendered
// depending on which side of the swap ran last. The newest claim wins — during
// a swap that is the incoming view, whichever order the commit runs in. Claims
// are idempotent per caller (each returns its own release, called at most once
// by the effect that made it).
type Claim = { slot: HTMLElement | null };

let claims: Claim[] = [];
const subscribers = new Set<() => void>();

function notify(): void {
  for (const fn of subscribers) fn();
}

// `slot` is the node in the listing's left column the crumb bar portals into.
// Optional: a caller that only wants the zone hidden (or a test) can claim
// without one, and the bar then stays where it is.
export function claimFolderChrome(slot: HTMLElement | null = null): () => void {
  const claim: Claim = { slot };
  claims.push(claim);
  notify();
  let released = false;
  return () => {
    if (released) return;
    released = true;
    const i = claims.indexOf(claim);
    if (i >= 0) claims.splice(i, 1);
    notify();
  };
}

export function folderChromeClaimed(): boolean {
  return claims.length > 0;
}

// The newest claim's slot, or null when nothing is claimed (or the claim came
// without one). Identity-stable, so it is safe as a useSyncExternalStore
// snapshot.
export function folderChromeSlot(): HTMLElement | null {
  return claims.length > 0 ? claims[claims.length - 1].slot : null;
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
  claims = [];
  notify();
}
