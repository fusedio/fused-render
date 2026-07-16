// Process-wide "is a blocking overlay open?" registry, shared across views.
//
// A directory opened in Preview embeds a Listing (Preview.tsx → <Listing/>),
// but the preview header's own context menu and dialogs live in SEPARATE React
// state that the embedded Listing can't see. Each view's document-level
// keyboard handlers only knew about their own overlay state, so with the
// preview menu open the embedded Listing's nav + file-op shortcuts
// (Cmd+Backspace / Cmd+X / Cmd+D …) still fired on a row behind it.
//
// Both views register here while any of their overlays (context menu or modal
// dialog) is open; Listing's nav + shortcut guards consult isOverlayOpen() so
// an overlay owned by EITHER view suppresses the other's keyboard handling.
//
// A COUNT, not a boolean: two views (or nested overlays) can be open at once,
// and each must release only its own hold — a shared boolean would let the
// first close re-enable shortcuts while another overlay is still up. Callers
// must pair every acquire with exactly one release (see the effects that use
// this: acquire on open, release on close AND on unmount).
let openCount = 0;

export function acquireOverlay(): void {
  openCount++;
}

export function releaseOverlay(): void {
  if (openCount > 0) openCount--;
}

export function isOverlayOpen(): boolean {
  return openCount > 0;
}
