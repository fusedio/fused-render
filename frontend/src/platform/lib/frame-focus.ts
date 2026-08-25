// The EMBEDDED-FRAME FOCUS CONTRACT: a page the shell renders for its own
// purposes — a preview in the explorer's pane, a live card thumbnail on /apps
// or Home — does not take the keyboard.
//
// An embedded frame is same-origin, so a page that focuses an input on boot —
// `autofocus`, or any `el.focus()` in its startup path — pulls document focus
// out of the shell. Two symptoms, one cause:
//
//   * THE PANE. The listing's arrow keys are document-level handlers that
//     stand down when focus is on a chrome control (useListingSelection), so
//     opening a preview stopped you browsing file to file with the keyboard:
//     Down/Up did nothing, because the keystrokes were going to the frame.
//   * THE CARD GRIDS (D348). Focusing an element inside a frame also SCROLLS
//     that frame into view, and the scroll propagates out to the embedder's
//     own scroll container. A card thumbnail that mounted mid-scroll and took
//     focus therefore yanked /apps to whatever row that card sits in — the
//     grid jumping to its end while the reader was still scrolling through it.
//     Thumbnails are display-only (a pointer-events shield keeps every click
//     on the card), so there is never a reason for one to hold focus at all.
//
// It is stated as a contract rather than patched per template, because the next
// template with an input would break it again:
//
//   1. The shell marks the embedded page's own URL with `_nofocus=1`, alongside
//      the `_file` / `_panelMode` / `_preview` conventions. Reserved
//      (`_`-prefixed), so it can never collide with a template's own params.
//   2. runtime.js — injected into EVERY rendered page — honours it: it strips
//      `autofocus`, and drops focus() calls until the reader has actually
//      interacted with the page. That is the half that covers templates nobody
//      has written yet.
//   3. The pane guards the door anyway (shouldReclaimFocus below). Same-origin
//      means the shell can see the frame take focus and simply take it back —
//      belt and braces for a page loaded from somewhere runtime.js does not
//      reach, and for the frames the browser focuses itself.
//   4. The CARD THUMBNAILS are SEALED, which is a different kind of answer
//      from 1–3 and the one that carries the weight for them (THUMB_SEAL at the
//      foot of this file): `sandbox` + an empty `allow`, attributes of the
//      EMBEDDER, which the page cannot opt out of and the shell does not have
//      to police. `autofocus` and autoplay stop applying at all, and the other
//      things a thumbnail could do to the page around it — navigate the shell
//      away, pop up a window, start a download, block everything with a
//      `confirm`, ask for the camera — stop being possible rather than being
//      undone afterwards.
//
//      This replaces a first attempt that put the guard in the SHELL: remember
//      where the card grid's scroller was, notice a frame displacing it, put it
//      back. That worked, and it was the wrong shape — the shell repairing
//      damage a thumbnail had already done, once per escape route, forever. The
//      containment belongs at the boundary and inside the frame, which is where
//      it now is. What is left of the scripted routes — `focus()`, `select()`,
//      `scrollIntoView()` — is handled in the page by runtime.js under this
//      flag, and `_nofocus=1` is inherited from a same-origin ancestor there,
//      so a page a thumbnail's page frames itself is covered too.
//
// Deliberate acts are untouched: clicking into the pane, tabbing into it, or
// expanding the preview full-screen all move focus for real. The contract is
// only about what a page may do on its OWN initiative, unprompted. A card
// thumbnail has no deliberate acts — its shield means a click is the card's.
//
// In `platform` rather than beside the pane it started with, because two apps
// now depend on it (`@apps/explorer`'s pane, `@apps/builder`'s cards) and an
// app may not import another app. Router-free and DOM-free either way, so the
// rules stay pinnable by a test that has neither.

// The param. Mirrored by name in static/runtime.js — keep the two in step.
export const NO_FOCUS_PARAM = "_nofocus";

// Mark an embedded page's URL as shell-mounted — a pane preview or a card
// thumbnail. Idempotent: both surfaces rebuild their src on every render, and a
// param that accumulated would change the URL and reload the frame for nothing.
export function withNoFocus(src: string): string {
  if (new URLSearchParams(src.split("?")[1] ?? "").get(NO_FOCUS_PARAM) === "1") return src;
  return src + (src.includes("?") ? "&" : "?") + NO_FOCUS_PARAM + "=1";
}

// The reader, for a query string with or without its leading "?" — the same
// question runtime.js asks about its own URL. Only the affirmative value counts,
// so `_nofocus=0` reads as "no" rather than as "present, therefore yes".
export function noFocusRequested(search: string): boolean {
  return new URLSearchParams(search.replace(/^\?/, "")).get(NO_FOCUS_PARAM) === "1";
}

// Should the pane take keyboard focus back? Exactly one question, asked when
// focus lands inside the preview frame:
//
//   • the frame has focus and the user has NOT reached into the pane → the page
//     took it on its own, which is the thing the contract forbids;
//   • anything else → leave it alone. Once the user has clicked or tabbed into
//     the pane, the frame owns the keyboard and every focus() the template makes
//     after that is on the user's behalf.
//
// "Has the user reached into the pane" is per PREVIEW, not per session: the pane
// remounts on every selection change (it is keyed on the previewed path), so the
// answer resets with the frame it is about.
export function shouldReclaimFocus(focusIsInFrame: boolean, userEngagedPane: boolean): boolean {
  return focusIsInFrame && !userEngagedPane;
}

// Is THIS Tab press the deliberate route into the pane — i.e. is the frame the
// very next stop in the direction the user is tabbing?
//
// The question exists because the shell answers it for the frame, and the
// answer is IRREVERSIBLE. A page under `_nofocus` suppresses focus by patching
// `HTMLElement.prototype.focus` and bouncing `focusin`, and runtime.js's
// `release` is one-shot and permanent: once the shell calls
// `__fusedReleaseNoFocus`, that preview may take the keyboard whenever it likes
// for as long as it is mounted. It has to be called BEFORE the focus moves —
// the bounce fires the instant focus lands, so releasing afterwards has already
// cost the user the tab stop they aimed at — which leaves the shell predicting
// rather than observing, and the prediction has to be narrow. "Any Tab in the
// shell" is not: Tab-cycling between the search box and a breadcrumb — nowhere
// near the pane — retired the runtime's half of the contract for good, and a
// template that focuses an input after a fetch then TOOK the keyboard and got
// bounced by the outer guard, instead of never taking it.
//
// `stops` is the document's focusable elements in tab order, `active` what has
// focus now. Not in the list (focus on <body>, the ordinary state after the
// guard has reclaimed) means the Tab starts from the top or the bottom, which
// is what the browser does with it.
//
// Positive `tabindex` is deliberately not modelled: it reorders the sequence
// ahead of document order, and it appears nowhere in this shell. Being wrong
// about it costs a release that does not happen — a tab stop lost once, which
// a click fixes — where the failure this replaces was permanent.
export function tabEntersFrame<T>(
  stops: readonly T[],
  active: T | null,
  frame: T,
  back: boolean,
): boolean {
  const i = active === null ? -1 : stops.indexOf(active);
  if (i < 0) return (back ? stops[stops.length - 1] : stops[0]) === frame;
  return stops[back ? i - 1 : i + 1] === frame;
}


// -- THE SEAL: what the browser enforces about a thumbnail -----------------------
//
// Spread onto every display-only thumbnail frame. It is the half of the contract
// that is NOT cooperation: the shell asks for nothing and the page cannot opt
// out, because these are the embedder's attributes rather than the page's code.
//
//   • `sandbox` — every restriction on, and only two lifted back. Scripts,
//     because a thumbnail of an app that cannot run is a blank box.
//     Same-origin, because the app has to be able to render: `/api`
//     (runPython, fused.ai, readFile) and the theme it reads out of
//     localStorage are both origin-bound, and an opaque origin turns every
//     data-driven card into its own empty state.
//
//     What the attribute's mere PRESENCE buys is the sandboxed automatic
//     features flag, for which no re-enabling token exists: `autofocus` never
//     applies and media never autoplays. That is the single biggest cause of
//     the /apps grid jumping (D486) closed by the browser, in markup, with no
//     runtime involved — and a thumbnail can no longer make noise either.
//
//     Everything not listed stays denied, and each one is something a
//     thumbnail could otherwise do TO the page around it: navigate the shell
//     away (`allow-top-navigation`), open a popup, start a download, submit a
//     form, take pointer lock, or raise an `alert`/`confirm`/`print` that
//     blocks the whole window (`allow-modals`).
//
//     `allow-scripts allow-same-origin` together is famously not a security
//     boundary — a frame with both can reach out through `parent` and delete
//     the sandbox attribute off its own iframe element — and it is not used as
//     one here. These are the reader's own local apps; the attribute is doing
//     feature containment, not privilege separation. The scripted escapes it
//     leaves open are closed inside the page instead, by runtime.js under
//     `_nofocus=1`.
//
//   • `allow=""` — an empty permissions policy: camera, microphone,
//     geolocation, display capture and fullscreen are all undelegated, so a
//     thumbnail cannot raise a permission prompt in the reader's name.
//
// Deliberately NOT here: `inert`. It reads like the answer — a picture should
// not be focusable — but the part that would matter, inertness propagating into
// the frame's own document, is the part being REMOVED from Blink (it is a
// cross-site leak and blocks fenced frames) and never existed in WebKit or
// Gecko (whatwg/html#7605). What it does cover — hit-testing and the tab order
// — these frames already have from the card's pointer shield and `tabIndex={-1}`.
export const THUMB_SEAL = {
  sandbox: "allow-scripts allow-same-origin",
  allow: "",
} as const;
