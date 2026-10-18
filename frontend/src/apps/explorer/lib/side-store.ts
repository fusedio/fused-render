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
// PERSISTED SINCE THE R1 CHAT FEEDBACK (#29), REVERSING WHAT THIS FILE USED TO
// SAY. It read "MEMORY ONLY, DELIBERATELY", on the argument that a refresh
// clearing the width is the escape hatch — a dragged column otherwise holds for
// the whole session, and the way back has to be something a user can find
// without being told about a gesture. The owner tested the drag against the
// legacy chat, found "the sidebar width change doesn't persist across reload"
// and asked for it: a width they set on purpose coming back undone by a reload
// reads as the app forgetting, not as a way out.
//
// So it is a preference now, and the escape hatch it cost is replaced by making
// a stored width UNABLE to strand anybody:
//   • only a COMPLETED drag is ever written (see `getSideWidth` below), so nothing
//     lands here that the user did not choose;
//   • a stored number is validated on read — finite, and at least `MIN_W` — so
//     a corrupted or hand-edited value can never open the column below the
//     width its content is legible at;
//   • the CEILING is still the container's, applied at the point of use on both
//     surfaces (`clampSideWidth` here, `paneFracFromSharedWidth` for the folder
//     pane), so a width stored on a wide monitor and reopened on a laptop is
//     narrowed to fit rather than swallowing the content pane. That was always
//     true; it is what makes persistence safe rather than merely wanted.
// The sibling `side-hidden-store.ts` (the OPEN/CLOSED flag) was later made a
// stored preference too, at the owner's request; its header says why.
//
// WHERE IT IS STORED: `explorerSide.width` in the server-held layout document
// (platform/lib/ui-layout.ts, D1386), read synchronously off that module's
// cache, which main.tsx fills before the first render. It was the localStorage
// key `fused-render:explorer-side-width` until the desktop app's port walk (a
// new origin, so empty storage, on most relaunches) was found resetting it —
// "across reload" had quietly stopped meaning "across relaunch". ui-layout.ts
// imports that key once. A failed write costs the persistence, never the drag:
// the cache is still the live answer and the column behaves exactly as before.
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

import { getUiLayout, patchUiLayout, subscribeUiLayout } from "@platform/lib/ui-layout";
import { MIN_W } from "./side-width";

/**
 * A stored width as a width this module will admit, or `null`.
 *
 * Exported for the test rather than reached through the store, because the
 * three ways this can go wrong are all data and none of them are the browser: a
 * non-number, a number below the legibility floor, and the absent case that has
 * to stay `null` (which is a REAL state — see `getSideWidth`) instead of
 * becoming a 0 the consumers would treat as a choice. Takes the string the old
 * localStorage key held as well as the document's number, so the tests that pin
 * the floor did not have to change with the storage.
 */
export function parseStoredSideWidth(raw: string | number | null | undefined): number | null {
  if (raw === null || raw === undefined || raw === "") return null;
  const px = Number(raw);
  if (!Number.isFinite(px)) return null;
  // Below the floor is not a width the column can be opened at (side-width.ts's
  // MIN_W is a measured legibility constraint, not a preference), so a stored
  // value under it is raised to it rather than honoured or discarded — the user
  // did drag narrow, and the floor is the narrowest that request can mean.
  return Math.max(MIN_W, Math.round(px));
}

// null = NO CHOICE MADE, which is a real state and not a missing number: the
// column then opens at the container's share (side-width.ts). Only a COMPLETED
// drag sets it — the resize clamp narrows what is on screen without recording a
// choice the user did not make, so widening the window back re-reads this number
// rather than the clamped one.
//
// READ THROUGH ON EVERY CALL rather than seeded into a module variable: the
// module is evaluated before main.tsx's hydrate lands, so a load-time seed would
// be the default every time. The two consumers read `getSideWidth()` in a
// `useState` initializer (PreviewSidebar, listing/pane), which runs after it.
const listeners = new Set<() => void>();

export function getSideWidth(): number | null {
  return parseStoredSideWidth(getUiLayout().explorerSide?.width);
}

export function setSideWidth(px: number | null): void {
  // Whole pixels: the document holds an int (the server 400s anything else),
  // and a sub-pixel difference is not a different choice.
  const next = px === null ? null : Math.round(px);
  if ((getUiLayout().explorerSide?.width ?? null) === next) return;
  patchUiLayout((d) => ({ ...d, explorerSide: { ...d.explorerSide, width: next } }));
  listeners.forEach((fn) => fn());
}

// A layout hydrate that landed after the first render (ui-layout.ts) may carry
// a width the mounted column has not seen; the subscribers re-read and clamp.
subscribeUiLayout(() => listeners.forEach((fn) => fn()));

/** Test seam: forget the width and what the store holds of it. Nothing in the app
 *  calls this — the app has no "reset the width" gesture, and inventing one
 *  here would be a UI decision made in a store. */
export function clearSideWidth(): void {
  setSideWidth(null);
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
