// BARE "/" FOCUSES THE CLAUDE CHAT — the shell-wide half of the shortcut.
//
// Platform may not import apps, so the two halves meet here: every mounted
// chat registers a "focus my composer" callback (ClaudeChat's ChatBody), and
// any surface that can OPEN a chat pane (Preview's file sidebar, Listing's
// folder pane) answers the `fused:open-chat` event by calling
// `preventDefault()` on it. App owns the one keydown listener (`handleSlashKey`).
//
// Order: focus a chat already on screen; else ask a host to open one, arming
// the next registration to take the caret once it mounts; else leave the key
// alone so the browser's quick-find still gets it.
import { isOverlayOpen } from "@platform/lib/ui-overlay";
import { anyModalOpen } from "@platform/ui/modal/esc-stack";

/** Dispatched on `document`, cancelable. A host that opens its chat pane for it
 *  calls `preventDefault()` — that is the "I handled it" answer. */
export const OPEN_CHAT_EVENT = "fused:open-chat";

/** How long an armed focus waits for a chat to mount and take it. */
const ARM_MS = 5000;
/** Retry cadence while an armed chat's box is not on screen yet. */
const RETRY_MS = 100;

type Focus = () => boolean;

// Insertion order = mount order; newest wins.
const composers = new Set<Focus>();
let armedUntil = 0;

function armed(): boolean {
  return armedUntil > Date.now();
}

/** Try the armed focus on `focus`, retrying until it lands, it unregisters, or
 *  the arm expires (the box can paint a frame after the chat mounts). */
function takeArmed(focus: Focus): void {
  if (!armed() || !composers.has(focus)) return;
  if (focus()) {
    armedUntil = 0;
    return;
  }
  setTimeout(() => takeArmed(focus), RETRY_MS);
}

/** A mounted chat registers its composer. `focus` focuses the textarea and
 *  returns true, or returns false when the box is not on screen. */
export function registerChatComposer(focus: Focus): () => void {
  composers.add(focus);
  if (armed()) takeArmed(focus);
  return () => {
    composers.delete(focus);
  };
}

/** Focus the newest chat composer that is on screen. */
export function focusAnyChatComposer(): boolean {
  for (const focus of [...composers].reverse()) {
    if (focus()) {
      armedUntil = 0;
      return true;
    }
  }
  return false;
}

/** The next chat to register takes the caret (once, within ARM_MS). Called by
 *  a host just before it opens its chat pane. */
export function armFocusOnRegister(): void {
  armedUntil = Date.now() + ARM_MS;
}

/** Ask a host to open its chat pane. True when one did. */
export function requestChatPane(): boolean {
  const ev = new CustomEvent(OPEN_CHAT_EVENT, { cancelable: true });
  document.dispatchEvent(ev);
  return ev.defaultPrevented;
}

/** Typing targets keep their "/" — a path in the search box, the chat itself. */
export function isEditableTarget(el: Element | null | undefined): boolean {
  if (!el) return false;
  const tag = (el.tagName ?? "").toUpperCase();
  if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return true;
  return !!(el as HTMLElement).isContentEditable;
}

/** The shell's bare-"/" handler. True when it took the key. */
export function handleSlashKey(e: KeyboardEvent): boolean {
  if (e.key !== "/") return false;
  if (e.ctrlKey || e.metaKey || e.altKey) return false;
  if (e.isComposing || e.defaultPrevented) return false;
  if (isEditableTarget(document.activeElement)) return false;
  if (isOverlayOpen() || anyModalOpen()) return false;
  if (focusAnyChatComposer() || requestChatPane()) {
    e.preventDefault();
    return true;
  }
  return false;
}

/** Test-only: drop every registration and any armed focus. */
export function resetChatFocusForTests(): void {
  composers.clear();
  armedUntil = 0;
}
