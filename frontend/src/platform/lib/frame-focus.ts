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
//   4. The CARDS guard theirs too (thumb-focus.ts) — which D348 said they did
//      not need. That reading was measured against a probe app doing the two
//      obvious things, an `autofocus` attribute and a `focus()` on load, and
//      step 2 does beat both. What it does not cover is the other routes into
//      focus: `input.select()`, `dialog.showModal()`, an engine that applies a
//      queued autofocus candidate before the bounce can matter, and — until
//      now — any page a thumbnail's page frames ITSELF, whose own URL carries
//      none of the shell's stamps (runtime.js inherits the flag from a
//      same-origin ancestor for exactly that). Every one of them ends in the
//      same place: the grid scrolled to a row the reader did not ask for. So
//      the card guard is written against the CONSEQUENCE rather than against
//      the list — the shell remembers what its card scroller was at, and puts
//      it back — and no future route into focus needs it changed.
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


// -- THE CARD GRID'S SCROLL, as arithmetic ------------------------------------
//
// A thumbnail that takes focus (or calls scrollIntoView) scrolls ITSELF into
// view, and the scroll walks out of the frame into the embedder's scroller: the
// /apps grid jumps to that card's row mid-scroll. By the time the shell hears
// about the focus the scroll has already happened — the focusing steps scroll
// first and fire the focus events after — so on this side the fix cannot be
// prevention. It is restoration: the shell remembers where its scroller was,
// and a frame that displaces it gets put back.
//
// "Remembers where it was" is the SCROLL EVENT's value, which is what makes the
// comparison below a fact rather than a guess. A scroll event is dispatched in
// a later rendering update than the offset change that caused it, so at the
// moment a frame steals focus the remembered value is still the last one the
// reader could see and the live value is already the displaced one. A
// difference therefore means "moved during this task, ahead of any scroll
// event" — programmatic, from inside the frame — and not "the reader is
// scrolling". The worst case if that ever slipped by a frame is that a reader
// mid-flick loses one frame of momentum, against a jump of thousands of pixels.
//
// The epsilon is for fractional offsets (scrollTop is not an integer at a
// fractional device pixel ratio): a sub-pixel difference is noise, and pinning
// against it would fight the reader over tenths of a pixel.
export const PIN_EPSILON_PX = 1;

export type ScrollOffset = { top: number; left: number };

// Which of a frame's candidate scrollers moved without the reader, and what to
// put each back to. DOM-free — the caller supplies both readings — so the rule
// stays pinnable by a test that has no layout.
export function displacedScrollers<T>(
  scrollers: readonly T[],
  remembered: (el: T) => ScrollOffset | undefined,
  live: (el: T) => ScrollOffset,
): { el: T; to: ScrollOffset }[] {
  const out: { el: T; to: ScrollOffset }[] = [];
  for (const el of scrollers) {
    const was = remembered(el);
    // No record means nothing to restore TO, and a restore to a
    // remembered-from-nowhere zero would be a worse jump than the one it is
    // meant to undo. The shell records a scroller the moment a thumbnail
    // registers, so this is the never-tracked case, not the never-scrolled one.
    if (!was) continue;
    const now = live(el);
    if (
      Math.abs(now.top - was.top) > PIN_EPSILON_PX ||
      Math.abs(now.left - was.left) > PIN_EPSILON_PX
    ) {
      out.push({ el, to: was });
    }
  }
  return out;
}
