// THE SIX LEGACY FRAME URLS, in one place, so the flag-off path is provably the
// same address it was before `ChatMount` existed.
//
// Each of these was an inline template string (or a shell helper) at its own
// call site; every one is copied VERBATIM, argument order and all, because the
// parity guard is byte-for-byte and a reordered `&_remote=1` is a different
// string even though it is the same request (00 §1a/§1b).
//
// The wrappers stay at the call sites, deliberately: `withNoFocus`
// (platform/lib/frame-focus) and `revSrc` (apps/explorer/lib/preview-rev) are
// facts about the HOST — a thumbnail shell, a git revision being previewed — and
// folding them in here would make six builders into twelve.
//
// Pure string functions, no DOM, no React: `legacy-src.test.ts` pins each one
// against the literal shape its old site produced.

/** Site 1 — Tasks → cards wall (`schedule-lib.ts:585-592 cardFrameSrc`).
 *  `compact=1` is this view's own: no top bar, no composer — the card's head
 *  says what the bar would, and a card is read rather than typed into. */
export function cardFrameSrc(template: string, target: string, sessionId: string): string {
  return (
    `/render?path=${encodeURIComponent(template)}` +
    `&_file=${encodeURIComponent(target)}` +
    `&chat_only=1&compact=1` +
    `&session_id=${encodeURIComponent(sessionId)}`
  );
}

/** Site 2 — Tasks → card popup (`schedule-lib.ts:602-609 peekFrameSrc`). Not
 *  `compact`: that cut hides the composer, which is the one thing the popup
 *  exists to give back. `peek=1` instead — the popup's head carries the doors. */
export function peekFrameSrc(template: string, target: string, sessionId: string): string {
  return (
    `/render?path=${encodeURIComponent(template)}` +
    `&_file=${encodeURIComponent(target)}` +
    `&chat_only=1&peek=1` +
    `&session_id=${encodeURIComponent(sessionId)}`
  );
}

/** Site 3 — the explorer file sidebar (`Preview.tsx:1618-1629 sideSrcFor`).
 *  `remote` and `thumbFlags` arrive as the pre-built `&…` fragments the host
 *  already had, and `_remote` sits BEFORE `chat_only` because that is the order
 *  the old expression concatenated them in. */
export function sideFrameSrc(
  template: string,
  target: string,
  remote: string,
  thumbFlags: string,
): string {
  return (
    `/render?path=${encodeURIComponent(template)}` +
    `&_file=${encodeURIComponent(target)}${remote}&chat_only=1${thumbFlags}`
  );
}

/** Site 4 — the explorer folder listing pane (`ListingPreviewPane.tsx:180-183`).
 *  `_noopen=1`, never `_preview=1` (D622): this pane is fully interactive, so it
 *  must not carry the display-only stamp that disables `fused.daemon.*` for
 *  every app it frames. The host still wraps this in `withNoFocus`. */
export function listingPaneSrc(template: string, folder: string, chatOnly: string): string {
  return (
    `/render?path=${encodeURIComponent(template)}` +
    `&_file=${encodeURIComponent(folder)}${chatOnly}&_noopen=1`
  );
}

/** Site 5 — the canvases workspace right pane (`CanvasWorkspace.tsx:335-338`).
 *  No `session_id` and no `run`: the template reads both through `fused.params`,
 *  i.e. off the `/canvases/<name>` URL, since this page sets no param boundary. */
export function canvasChatSrc(template: string, dir: string): string {
  return (
    `/render?path=${encodeURIComponent(template)}` +
    `&_file=${encodeURIComponent(dir)}&chat_only=1`
  );
}

/** Site 6 — the explorer CONTENT pane, `_mode=claude` as the main body
 *  (`Preview.tsx:1573-1585 srcFor`). No `chat_only`, which is the whole point:
 *  the template renders its own two-column split, its left half being its own
 *  preview of `_file`. The host still wraps this in `revSrc`. */
export function contentModeSrc(
  template: string,
  fsPath: string,
  remote: string,
  thumbFlags: string,
): string {
  return (
    `/render?path=${encodeURIComponent(template)}` +
    `&_file=${encodeURIComponent(fsPath)}${remote}${thumbFlags}`
  );
}
