// The preview SIDEBAR's dragged width, for the lifetime of the DOCUMENT: one
// PIXEL number, held in a module variable and written to no storage at all —
// and, since D460, shared by BOTH companion columns the app has: the file
// view's sidebar (PreviewSidebar.tsx, this module's original and still its
// only direct writer of `SideCloseButton`-adjacent UI) and the folder
// listing's preview pane (`listing/pane.ts`'s `usePreviewPane`). A drag on
// either one is visible on the other for the rest of the session.
//
// The two surfaces already shared the undragged DEFAULT (`companionFrac`,
// `lib/side-width.ts`, D283) while remembering a drag independently — this
// pane's own `listing/pane-store.ts` held a FRACTION of its container, deleted
// now that both read and write this same pixel number. The unit stayed pixels
// rather than becoming a shared fraction because the sidebar's floors
// (`MIN_W`/`CONTENT_MIN_W`, `lib/side-width.ts`) are legibility constraints
// stated in pixels with no honest fractional form — a chat composer's control
// row either fits on one line at this width or it does not, regardless of how
// wide the window around it happens to be — while a fraction-of-container
// converts to pixels losslessly given the width at hand. The folder pane's own
// (narrower) floors are applied to this same stored number on ITS side of the
// read, in `listing/pane-math.ts`'s `paneFracFromSharedWidth` — sharing the
// stored value does not mean sharing the floor.
//
// IT USED TO BE `useState` INSIDE PreviewSidebar, and that was the bug. StatView
// is keyed by path (shell/App.tsx), so the whole preview — this column included —
// REMOUNTS on every file→file navigation, and a mount-local width means the
// divider springs back to the default share the moment you arrow onto the next
// file. The listing pane never had that problem because its width had already been
// lifted out of the component; this is the file half catching up (D326), and the
// two surfaces now behave identically under navigation — and, since D460, share
// the very same number rather than two independently-remembered ones.
//
// MEMORY ONLY, DELIBERATELY — not a missing feature:
//   • it survives everything the SHELL does, because the shell navigates by
//     history.pushState (platform/lib/router) and never reloads the document.
//     File → file, file → folder, Back and Forward all keep the width.
//   • a REFRESH clears it and the column opens at the companion share again
//     (lib/side-width's `defaultSideWidth`). That reset is the escape hatch: a
//     dragged width otherwise holds for the whole session, and the way back has to
//     be something a user can find without being told about a gesture. Reloading
//     a page is that — and it is the same escape hatch the sidebar's OPEN/CLOSED
//     state has (lib/preview-side), so one refresh returns the whole column to its
//     stated starting shape rather than half of it.
// sessionStorage would survive the refresh and localStorage the browser, so both
// would take the escape hatch away. Neither is an option here.
//
// WHY THIS IS PIXELS, AND WHY IT IS NOW THE ONLY STORE. Until D460 the listing
// pane kept its own store (`listing/pane-store.ts`, deleted) holding a FRACTION
// of its container (0…1), on the reasoning that a conversion between the two
// units needs a container width neither module wanted to depend on — so they
// shared only the undragged DEFAULT (`companionFrac`, D283) and remembered a
// drag independently. D460 decided that reasoning was solving the wrong
// problem: the CONTAINER WIDTH IS ALWAYS AVAILABLE AT THE POINT OF USE (the
// folder pane already measures its own split container for the default share;
// see `listing/pane.ts`'s `useSplitWidth`), so the conversion this module used
// to avoid is trivial exactly where it is needed and nowhere else. This store
// now holds ONE PIXEL NUMBER for both surfaces; the folder pane converts it to
// a fraction of its own container on every read (`listing/pane-math.ts`'s
// `paneFracFromSharedWidth`), clamped into its OWN floors rather than this
// column's — sharing the number does not mean sharing the floor.

// null = NO CHOICE MADE, which is a real state and not a missing number: the
// column then opens at the container's share (side-width.ts). Only a COMPLETED
// drag sets it — the resize clamp narrows what is on screen without recording a
// choice the user did not make, so widening the window back re-reads this number
// rather than the clamped one.
let chosen: number | null = null;
const listeners = new Set<() => void>();

export function getSideWidth(): number | null {
  return chosen;
}

export function setSideWidth(px: number | null): void {
  if (chosen === px) return;
  chosen = px;
  listeners.forEach((fn) => fn());
}

// WHY THIS STORE NOW NOTIFIES, when for its whole life it was a bare variable
// read once at mount. The REOPEN drag is the reason, and it is the one gesture
// where the thing being resized is not the thing holding the pointer.
//
// Pulling a shut column open (SideReopenEdge) starts on a strip that exists only
// while the column is shut, so the instant the pull crosses its threshold the
// column mounts and the strip is unmounted out from under the still-running
// drag. The gesture itself survives that — capture is taken on
// documentElement, the same trick a row drag uses for the same reason
// (listing/row-drag.ts) — but the widths it goes on producing have nowhere to
// land: PreviewSidebar seeds from `getSideWidth()` once and never looks again.
// So the second half of the drag would be silent, the cursor walking away from
// an edge that had stopped following it.
//
// Making the store the channel is what closes that gap: the strip writes every
// move here, and the column subscribes. It costs the same set-of-callbacks the
// sidebar's own store already uses (platform/lib/sidebarstate), and it keeps the
// handoff to ONE fact — the width — rather than a second live-drag protocol
// between two components that never render together.
export function subscribeSideWidth(fn: () => void): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}
