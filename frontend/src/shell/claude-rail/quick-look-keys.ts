// THE SHIFT KEY, HELD (owner, 2026-11-10; design.md "Quick look v2": "Space
// messes my typing → Shift for now"): the shell's one key-hold. Shift down
// (left or right) holds quick look (quick-look-store); Shift up lets it go,
// every window back to its pinned chat.
//
// A LOST KEYUP ENDS IT TOO: a keyup that lands elsewhere — another app, a
// frame, a system shortcut — never reaches us, and a hold left standing would
// keep a preview up for a key nobody holds. So the window losing focus, the
// page going hidden, and ANY key or pointer event that says Shift is not down
// (`shiftKey === false`) while held all release.
//
// No field guard: Shift alone types nothing, and holding it over a ring
// previews whatever has the caret (the composer keeps it; the preview takes
// none). An auto-repeat is not a new press. Escape while a window previews
// sends it back to pinned and is nobody else's; the hold goes on.
//
// ON THE WINDOW, CAPTURE PHASE: ahead of every document listener, so that
// Escape is taken before the cards' own Escape handlers see it.
import { holdQuickLook, quickLookHeld, releaseQuickLook, revertPreviews } from "./quick-look-store";

/** The parts of a keydown the hold reads. */
export type HoldEvent = Pick<KeyboardEvent, "key" | "repeat" | "shiftKey" | "isComposing">;

/** Whether this keydown starts a hold: Shift, not a repeat, not mid-IME. */
export function shiftHolds(e: HoldEvent): boolean {
  return e.key === "Shift" && !e.repeat && !e.isComposing;
}

interface KeyHost {
  addEventListener(type: string, fn: (e: Event) => void, opts?: boolean | AddEventListenerOptions): void;
  removeEventListener(type: string, fn: (e: Event) => void, opts?: boolean | AddEventListenerOptions): void;
}

/** Install the key layer; the answer takes it off (and lets any hold go). */
export function installQuickLookKeys(win: KeyHost = window, doc: Document = document): () => void {
  const onDown = (ev: Event) => {
    const e = ev as KeyboardEvent;
    if (quickLookHeld()) {
      // The lost keyup (header).
      if (!e.shiftKey) {
        releaseQuickLook();
        return;
      }
      if (e.key === "Escape" && !e.defaultPrevented && revertPreviews()) {
        e.preventDefault();
        e.stopPropagation();
      }
      return;
    }
    if (shiftHolds(e)) holdQuickLook();
  };
  const onUp = (ev: Event) => {
    const e = ev as KeyboardEvent;
    // Shift's own keyup — unless the OTHER Shift is still down (`shiftKey`
    // still true: two Shift keys) — or any other key's once Shift is up (its
    // keyup went elsewhere).
    if (quickLookHeld() && !e.shiftKey) releaseQuickLook();
  };
  const onPointer = (ev: Event) => {
    if (quickLookHeld() && !(ev as PointerEvent).shiftKey) releaseQuickLook();
  };
  const onBlur = () => {
    if (quickLookHeld()) releaseQuickLook();
  };
  const onVisibility = () => {
    if (doc.visibilityState === "hidden" && quickLookHeld()) releaseQuickLook();
  };
  win.addEventListener("keydown", onDown, true);
  win.addEventListener("keyup", onUp, true);
  win.addEventListener("pointermove", onPointer, true);
  win.addEventListener("pointerdown", onPointer, true);
  win.addEventListener("blur", onBlur);
  doc.addEventListener("visibilitychange", onVisibility);
  return () => {
    win.removeEventListener("keydown", onDown, true);
    win.removeEventListener("keyup", onUp, true);
    win.removeEventListener("pointermove", onPointer, true);
    win.removeEventListener("pointerdown", onPointer, true);
    win.removeEventListener("blur", onBlur);
    doc.removeEventListener("visibilitychange", onVisibility);
    if (quickLookHeld()) releaseQuickLook();
  };
}
