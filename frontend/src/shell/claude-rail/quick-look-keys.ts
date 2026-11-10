// THE SPACE KEY, HELD (owner, 2026-11-10; design.md "Space quick look"): the
// one key-hold the shell has. Space down holds quick look (quick-look-store),
// Space up lets it go; so do the window losing focus and the page going
// hidden, since a keyup that happens elsewhere never reaches us and a card
// left standing for a key nobody holds would be a card nobody can close.
//
// WHEN SPACE IS SPACE AND NOT A HOLD (`spaceHolds`), copied from the bare-"/"
// guard (platform/lib/chat-focus `handleSlashKey`): a modifier, an IME
// composition, a key someone already took, the caret in a field (typing a
// space), the focus on a control (Space presses a button, follows a link,
// toggles a row), a dialog or menu open over the page, an auto-repeat — or no
// rail on screen to aim at, where Space keeps scrolling the page as it always
// did. Otherwise the hold takes the key (`preventDefault`): no page scroll,
// and the explorer's type-to-search (useListingSelection) sees it taken.
//
// ON THE WINDOW, CAPTURE PHASE: ahead of every document listener, so an
// Escape while the card shows is the card's alone — window A's card, the +
// popover and the Modal chassis all read `defaultPrevented` or never hear it.
import { isEditableTarget } from "@platform/lib/chat-focus";
import { isOverlayOpen } from "@platform/lib/ui-overlay";
import { dismissQuickLook, holdQuickLook, quickLookState, releaseQuickLook } from "./quick-look-store";

/** The parts of a keydown the guard reads. */
export type SpaceKey = Pick<
  KeyboardEvent,
  "key" | "repeat" | "ctrlKey" | "metaKey" | "altKey" | "shiftKey" | "isComposing" | "defaultPrevented"
>;

/** An element as the guard walks it (the DOM's, or a test's stand-in). */
export interface KeyTarget {
  tagName?: string;
  classList?: { contains(name: string): boolean };
  isContentEditable?: boolean;
  getAttribute?(name: string): string | null;
  parentElement?: KeyTarget | null;
}

const PRESSABLE_TAGS = new Set(["BUTTON", "A", "SUMMARY", "SELECT", "OPTION", "LABEL"]);
const PRESSABLE_ROLES = new Set([
  "button",
  "link",
  "menuitem",
  "menuitemcheckbox",
  "menuitemradio",
  "option",
  "checkbox",
  "radio",
  "switch",
  "tab",
  "treeitem",
  "row",
  "gridcell",
]);

/** The focus is on something Space presses or types into — it, or a control
 *  it sits inside. */
export function isSpaceTarget(el: KeyTarget | null | undefined): boolean {
  if (isEditableTarget(el as Element | null | undefined)) return true;
  for (let n: KeyTarget | null | undefined = el; n; n = n.parentElement) {
    if (PRESSABLE_TAGS.has((n.tagName ?? "").toUpperCase())) return true;
    if (n.isContentEditable) return true;
    const role = n.getAttribute?.("role");
    if (role && PRESSABLE_ROLES.has(role)) return true;
    const editable = n.getAttribute?.("contenteditable");
    if (editable !== null && editable !== undefined && editable !== "false") return true;
  }
  return false;
}

/** The focus is inside the Claude rail itself (`.claude-rail`). */
export function inRail(el: KeyTarget | null | undefined): boolean {
  for (let n: KeyTarget | null | undefined = el; n; n = n.parentElement) {
    if (n.classList?.contains("claude-rail")) return true;
  }
  return false;
}

/** Whether this keydown starts a hold (header). `blocked`: a dialog, menu
 *  or overlay is open; `railUp`: a rail stands to aim at.
 *  THE RAIL'S OWN FOCUS IS NO CONTROL'S (review, 2026-11-10): a ring is an
 *  `<a>`, and the press that opened its chat leaves the focus on it — the
 *  pressable-target rule would kill Space until the focus moved. Inside the
 *  rail only a field keeps its Space; buttons and links elsewhere still do. */
export function spaceHolds(e: SpaceKey, ctx: { active: KeyTarget | null; blocked: boolean; railUp: boolean }): boolean {
  if (e.key !== " ") return false;
  if (e.repeat) return false;
  if (e.ctrlKey || e.metaKey || e.altKey || e.shiftKey) return false;
  if (e.isComposing || e.defaultPrevented) return false;
  if (ctx.blocked || !ctx.railUp) return false;
  if (isEditableTarget(ctx.active as Element | null | undefined)) return false;
  if (inRail(ctx.active)) return true;
  return !isSpaceTarget(ctx.active);
}

/** A REAL dialog over the page — the Modal chassis' modal form (not its
 *  popover form: the + popover and the bot card are cards beside the rail,
 *  exactly what quick look is for looking past), a shadcn dialog — or any
 *  overlay holding the lock (ui-overlay). */
export const BLOCKING_DIALOGS = '[aria-modal="true"], [data-slot="dialog-content"], [data-slot="alert-dialog-content"]';

/** …ONE ON SCREEN (review, 2026-11-10): a dialog kept in the DOM while
 *  hidden (`display: none`, a closed shell) draws no box and must not kill
 *  Space. */
function blockedIn(doc: Document): boolean {
  if (isOverlayOpen()) return true;
  return Array.from(doc.querySelectorAll(BLOCKING_DIALOGS)).some((el) => el.getClientRects().length > 0);
}

interface KeyHost {
  addEventListener(type: string, fn: (e: Event) => void, opts?: boolean | AddEventListenerOptions): void;
  removeEventListener(type: string, fn: (e: Event) => void, opts?: boolean | AddEventListenerOptions): void;
}

/** Install the key layer; the answer takes it off (and lets any hold go). */
export function installQuickLookKeys(
  win: KeyHost = window,
  doc: Document = document,
): () => void {
  const onDown = (ev: Event) => {
    const e = ev as KeyboardEvent;
    // ⌘ AFTER SPACE (review, 2026-11-10): macOS sends no keyup for a key
    // pressed under ⌘, so a Space up after ⌘ never arrives and the hold
    // would never end — Space swallowed for good. A ⌘ means the hold is over.
    if (e.metaKey && quickLookState().held) {
      releaseQuickLook();
      return;
    }
    if (e.key === "Escape") {
      if (!quickLookState().held || e.defaultPrevented) return;
      // An aim still waiting out its delay is called off too (review,
      // 2026-11-10); the key is the card's alone only when a card showed —
      // before anyone else's (header).
      const shown = quickLookState().key !== null;
      dismissQuickLook();
      if (!shown) return;
      e.preventDefault();
      e.stopPropagation();
      return;
    }
    if (e.key !== " ") return;
    // HELD ALREADY: the auto-repeat is ours too, or the page would scroll
    // under the card. A FRESH Space down (not a repeat) while held proves the
    // hold stale — its keyup went somewhere we never heard: let it go, then
    // ask again as for any first press.
    if (quickLookState().held) {
      if (e.repeat) {
        e.preventDefault();
        return;
      }
      releaseQuickLook();
    }
    const ok = spaceHolds(e, {
      active: doc.activeElement as KeyTarget | null,
      blocked: blockedIn(doc),
      railUp: !!doc.querySelector(".claude-rail"),
    });
    if (!ok) return;
    e.preventDefault();
    holdQuickLook();
  };
  const onUp = (ev: Event) => {
    const e = ev as KeyboardEvent;
    if (!quickLookState().held) return;
    // ⌘'s own keyup, for the same lost Space up (above).
    if (e.metaKey || e.key === "Meta") {
      releaseQuickLook();
      return;
    }
    if (e.key !== " ") return;
    e.preventDefault();
    releaseQuickLook();
  };
  const onBlur = () => {
    if (quickLookState().held) releaseQuickLook();
  };
  const onVisibility = () => {
    if (doc.visibilityState === "hidden" && quickLookState().held) releaseQuickLook();
  };
  win.addEventListener("keydown", onDown, true);
  win.addEventListener("keyup", onUp, true);
  win.addEventListener("blur", onBlur);
  doc.addEventListener("visibilitychange", onVisibility);
  return () => {
    win.removeEventListener("keydown", onDown, true);
    win.removeEventListener("keyup", onUp, true);
    win.removeEventListener("blur", onBlur);
    doc.removeEventListener("visibilitychange", onVisibility);
    if (quickLookState().held) releaseQuickLook();
  };
}
