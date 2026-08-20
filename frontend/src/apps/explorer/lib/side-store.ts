// The file preview SIDEBAR's dragged width, for the lifetime of the DOCUMENT:
// one number shared by every file, held in a module variable and written to no
// storage at all. The exact policy `listing/pane-store.ts` states for the folder
// listing's pane, and that module's header is the long version of the argument —
// read it there; only what is DIFFERENT is written down here.
//
// IT USED TO BE `useState` INSIDE PreviewSidebar, and that was the bug. StatView
// is keyed by path (shell/App.tsx), so the whole preview — this column included —
// REMOUNTS on every file→file navigation, and a mount-local width means the
// divider springs back to the default share the moment you arrow onto the next
// file. The listing pane never had that problem because its width had already been
// lifted out of the component; this is the file half catching up (D326), and the
// two surfaces now behave identically under navigation.
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
// WHY THIS IS A SECOND STORE AND NOT `pane-store`'s: the two hold different
// QUANTITIES. The listing pane records a FRACTION of its container (0…1 —
// listing/pane-math.ts), because a listing beside a preview is a proportion of the
// window; this column records PIXELS, because what it holds is a chat composer and
// a message column whose legibility is a width in pixels (lib/side-width's
// floors). Sharing one variable would have to pick one unit and convert at every
// read, and a conversion needs the container width — which is exactly the
// measurement neither store wants to depend on. They share the DEFAULT instead
// (`companionFrac`, D283), which is the part that actually has to agree: the two
// columns open at the same share, and each remembers a drag in its own terms.

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
