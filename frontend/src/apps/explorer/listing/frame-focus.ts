// The preview pane's FOCUS CONTRACT: a preview rendered in the pane does not
// take the keyboard.
//
// The pane is a same-origin iframe, so a page that focuses an input on boot —
// `autofocus`, or any `el.focus()` in its startup path — pulls document focus
// out of the shell. The listing's arrow keys are document-level handlers that
// stand down when focus is on a chrome control (useListingSelection), so the
// symptom is that opening a preview stops you browsing file to file with the
// keyboard: Down/Up do nothing, because the keystrokes are going to the frame.
//
// It is stated as a contract rather than patched per template, because the next
// template with an input would break it again:
//
//   1. The shell marks the preview's own URL with `_nofocus=1`, alongside the
//      `_file` / `_panelMode` conventions. Reserved (`_`-prefixed), so it can
//      never collide with a template's own params.
//   2. runtime.js — injected into EVERY rendered page — honours it: it strips
//      `autofocus`, and drops focus() calls until the reader has actually
//      interacted with the page. That is the half that covers templates nobody
//      has written yet.
//   3. The pane guards the door anyway (shouldReclaimFocus below). Same-origin
//      means the shell can see the frame take focus and simply take it back —
//      belt and braces for a page loaded from somewhere runtime.js does not
//      reach, and for the frames the browser focuses itself.
//
// Deliberate acts are untouched: clicking into the pane, tabbing into it, or
// expanding the preview full-screen all move focus for real. The contract is
// only about what a page may do on its OWN initiative, unprompted.
//
// Router-free and DOM-free, like the pane's other decision modules, so the two
// rules can be pinned by a test that has neither.

// The param. Mirrored by name in static/runtime.js — keep the two in step.
export const NO_FOCUS_PARAM = "_nofocus";

// Mark a preview URL as pane-embedded. Idempotent: the pane rebuilds its src on
// every render, and a param that accumulated would change the URL and reload
// the frame for nothing.
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
