// The dirty-form discard guard's decision logic, pulled out of Modal.tsx so the
// rules can be tested without a DOM renderer (same split as lib/exit-animation).
//
// The guard is a two-press latch. On a dirty form the first ✕/Esc/backdrop press
// ARMS — the ✕ goes amber and a hint says what the next press will do — and the
// next press discards.
//
// WHAT THIS FIXES. Arming used to lapse on a 2s timer, and a press arriving
// after the lapse re-armed instead of closing. Pressing ✕ slower than every two
// seconds therefore looped forever, and a modal with no Cancel button had no
// exit but Save (QA, 2026-08-18: presses at 2.6/5.2/7.8/10.4s all left the
// dialog open). Nothing here is time-dependent any more — that is the fix, and
// it is why this module takes no clock.
//
// What ends the armed state instead is the user answering the question the
// other way: going back to the form. See `isDisarmingInteraction`.

// The close control lives INSIDE the dialog, so the disarm listener has to be
// able to tell "the user went back to editing" from "the user pressed ✕ again".
export const CLOSE_CONTROL_SELECTOR = ".modal-close";

// What a close attempt (✕ / Esc / backdrop) should do right now.
//   "close" — run the close, discarding the form
//   "arm"   — intercept and ask first
//   "block" — the modal is busy; the attempt does nothing
export type CloseDecision = "close" | "arm" | "block";

export function decideClose(opts: {
  busy: boolean;
  dirty: boolean;
  armed: boolean;
}): CloseDecision {
  if (opts.busy) return "block";
  // Already armed → always close, no matter how long the user took deciding.
  // This is the whole defect: there is no elapsed-time term in this expression.
  if (opts.dirty && !opts.armed) return "arm";
  return "close";
}

// Keys that must NOT disarm, even though they are keystrokes inside the dialog:
//   Escape — that IS the second press
//   Tab / Shift — navigating back to the ✕ to press it with the keyboard must
//     not undo the arming on the way there
const NON_DISARMING_KEYS = new Set(["Escape", "Tab", "Shift"]);

// Whether an interaction inside the dialog means "no, I'm still editing" and so
// should reset the guard to unarmed.
//
// `key` is the keyboard key for a keydown, or null for a pointer/input/change
// event. `insideCloseControl` is whether the event's target sits within the ✕
// (i.e. `target.closest(CLOSE_CONTROL_SELECTOR)`).
export function isDisarmingInteraction(
  key: string | null,
  insideCloseControl: boolean,
): boolean {
  if (insideCloseControl) return false;
  if (key !== null && NON_DISARMING_KEYS.has(key)) return false;
  return true;
}

// ---- the backdrop grace -----------------------------------------------------
//
// A PRESS ON THE SCRIM RIGHT AFTER THE DIALOG OPENED IS NOT A DISMISSAL. The
// dialog is opened from a click — a menu row, a trash button — and the pointer
// is still parked on that spot when the dialog paints somewhere else. A second
// press there (a double-click habit, a trackpad that registered two presses)
// lands on the scrim, and until now that closed the dialog it had just opened:
// a "Delete task" confirm that appeared and vanished in under 200ms, inside its
// own enter animation, so without even a fade (Sina, 2026-09-19, three
// recordings; reproduced in headless Chromium with a second press at 100–180ms).
//
// So the scrim ignores presses in its first `BACKDROP_GRACE_MS`. The window is
// wider than any double-click interval (macOS's slowest setting is ~500ms) and
// shorter than a deliberate "I read this and want out" — that press comes after
// the dialog has been looked at. Escape, ✕ and Cancel are untouched: none of
// them can be hit by accident from where the pointer already was.
export const BACKDROP_GRACE_MS = 500;

/** Should a mousedown on the scrim at `now` close a dialog opened at `openedAt`? */
export function backdropPressCloses(openedAt: number, now: number): boolean {
  return now - openedAt >= BACKDROP_GRACE_MS;
}
