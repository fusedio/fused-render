// The plan popup's "click/Enter/Space opens it" guard (D890) — shared by the
// card's two open affordances (PlanCard.tsx) and the historical chip's own
// (ToolChip.tsx's PlanChipBody), so there is one place this logic can be
// wrong rather than two copies that drift (code review finding 3).
//
// Two bugs the guard exists to close (code review findings 1-2), both from the
// SAME cause: the plan body's own click/key handler sits on a wrapper `div`
// around content that has its own real interactive children — a code block's
// copy button (MarkdownView's `enhanceCodeBlocks`, a plain DOM `<button>` it
// creates imperatively, not a React one). A click or a focused Enter/Space on
// that button bubbles to the wrapper same as any other click would:
//
//   1. A click LANDS on the button, bubbles to the wrapper, and the wrapper's
//      `onClick` opens the modal on top of the copy.
//   2. Enter/Space on the FOCUSED button is worse: the wrapper's `onKeyDown`
//      called `preventDefault()` unconditionally, which also killed the
//      button's own native activation.
//   3. Selecting plan text to copy it ends the drag with a `mouseup`, which is
//      a `click` too — the wrapper can't tell that click apart from an
//      ordinary one without asking whether a selection survived it.
import type { KeyboardEvent as ReactKeyboardEvent, MouseEvent as ReactMouseEvent } from "react";

/** True when the event's target IS (or sits inside) a real interactive
 *  element — walked with `closest`, not `e.target === e.currentTarget`: the
 *  plan body's own prose (a `<p>`, a `<li>`) is not `e.currentTarget` either,
 *  and clicking THAT still has to open the popup. Only an actual button/link/
 *  form control gets to keep the event for itself. */
export function isInteractiveDescendant(e: { target: EventTarget | null }): boolean {
  const el = e.target as { closest?: (selector: string) => unknown } | null;
  return !!el?.closest?.("button, a, input, textarea, select");
}

/** True while the window holds a real (non-collapsed) text selection — the
 *  `mouseup` that ends a copy-drag inside the plan counts as a `click`, and
 *  that click must not also pop the modal open on top of the selection. */
export function hasActiveSelection(): boolean {
  try {
    const sel = typeof window !== "undefined" ? window.getSelection?.() : null;
    return !!sel && !sel.isCollapsed && sel.toString().length > 0;
  } catch {
    // No `getSelection` (or a hostile environment) — nothing to guard against.
    return false;
  }
}

/** The click guard for the plan body / "Open" affordances: skip a click that
 *  bubbled from a real interactive descendant, and skip a click that is
 *  really the tail end of a text-selection drag. */
export function guardedOpenOnClick(fn: () => void) {
  return (e: ReactMouseEvent) => {
    if (isInteractiveDescendant(e)) return;
    if (hasActiveSelection()) return;
    fn();
  };
}

/** Enter/Space activate a non-`<button>` clickable the same way a real button
 *  would — used by the plan body and the "Open" affordance in both PlanCard
 *  and the historical ToolChip. Guarded the same way the click is: a focused
 *  copy button's own Enter/Space must reach ITS handler, not the wrapper's,
 *  so `preventDefault` is never called for an event that is not this
 *  control's to take. */
export function activateOnKey(fn: () => void) {
  return (e: ReactKeyboardEvent) => {
    if (e.key !== "Enter" && e.key !== " ") return;
    if (isInteractiveDescendant(e)) return;
    e.preventDefault();
    fn();
  };
}
