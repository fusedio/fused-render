// CHAT TEXT ZOOM — ⌘= / ⌘+ bigger, ⌘− smaller, ⌘0 actual size, while focus is
// inside a Claude chat (design.md, agent/20261110-chat-font-zoom).
//
// ONE GLOBAL FACTOR, written on <html> as `--c-chat-zoom`. Only the chat's own
// roots read it (`styles/chat.css`: `.chat-root, .c-tokens, .c-overlay
// { --c-zoom: var(--c-chat-zoom, 1) }`), and every px font-size / line-height
// in apps/claude/styles is `calc(Npx * var(--c-zoom, 1))` — so every chat on
// the page resizes at once and nothing else in the shell moves. A compact wall
// tile pins `--c-zoom: 1` (it is a thumbnail).
//
// PERSISTED in the server-held ui-layout doc (`chatTextScale`), so it survives a
// relaunch on another port; other documents of this origin (a second window, an
// embed) hear a press on a BroadcastChannel and follow without a reload.
//
// ONE document keydown listener, CAPTURE phase: it must see the chord before a
// composer's own React keydown handlers (delegated at the root container, which
// is below `document`) get a chance to swallow it. It claims — preventDefault —
// only a chord it acts on, and only when the press belongs to a chat.
import { isMod } from "@platform/lib/platform";
import {
  CHAT_TEXT_SCALES,
  getUiLayout,
  patchUiLayout,
  snapChatTextScale,
  subscribeUiLayout,
} from "@platform/lib/ui-layout";

/** The surfaces that ARE a chat for this shortcut: the chat roots and the
 *  chat's portaled popups (slash menu, file preview, plan modal). */
const CHAT_SCOPE = ".chat-root, .c-tokens, .c-overlay";
const CHANNEL = "fused-render:chat-zoom";
const CSS_VAR = "--c-chat-zoom";

/** +1 bigger, -1 smaller, 0 actual size. */
export type ChatZoomStep = 1 | -1 | 0;

type KeyLike = {
  key: string;
  code: string;
  metaKey: boolean;
  ctrlKey: boolean;
  altKey: boolean;
  shiftKey: boolean;
  isComposing?: boolean;
};

/** Which zoom chord `e` is, or null. The platform's primary modifier only
 *  (`isMod`: ⌘ on macOS, Ctrl elsewhere), never with ⌥; Shift is allowed for
 *  `+` / `_` (they ARE shifted keys on most layouts) but not for 0. Both the
 *  character and the physical key are read, so a layout that moves `=` still
 *  has the chord. */
export function chatZoomChord(e: KeyLike): ChatZoomStep | null {
  if (e.isComposing || !isMod(e) || e.altKey) return null;
  const { key, code } = e;
  if (key === "=" || key === "+" || code === "Equal" || code === "NumpadAdd") return 1;
  if (key === "-" || key === "_" || code === "Minus" || code === "NumpadSubtract") return -1;
  if (!e.shiftKey && (key === "0" || code === "Digit0" || code === "Numpad0")) return 0;
  return null;
}

/** The factor one step from `current` (snapped first); 0 = back to 1. Clamped
 *  at both ends of `CHAT_TEXT_SCALES`. */
export function stepChatTextScale(current: number, step: ChatZoomStep): number {
  if (step === 0) return 1;
  const i = CHAT_TEXT_SCALES.indexOf(snapChatTextScale(current));
  const j = Math.min(CHAT_TEXT_SCALES.length - 1, Math.max(0, i + step));
  return CHAT_TEXT_SCALES[j]!;
}

type Closest = { closest?: (sel: string) => Closest | null };

/** Does `target` sit inside a chat (and not a compact wall tile, which never
 *  zooms)? Duck-typed on `closest` so a test can hand it a plain object. */
export function isInChat(target: unknown): boolean {
  const el = target as Closest | null;
  if (!el || typeof el.closest !== "function") return false;
  const root = el.closest(CHAT_SCOPE);
  if (!root) return false;
  return !(typeof root.closest === "function" && root.closest(".chat-compact"));
}

// ---- state -----------------------------------------------------------------

let scale = 1;
let installed = false;
/** The last thing pressed: transcript text is not focusable, so a reader who
 *  clicked into a reply and pressed ⌘+ fires keydown at <body>. That press is
 *  the chat's when the click was. */
let lastPointer: EventTarget | null = null;

/** This document's current factor (1 = 100 %). */
export function getChatTextScale(): number {
  return scale;
}

/** Fired on `window` after the factor changed, once the new size is on the
 *  page: a box that sized itself to its text (`autoGrow.ts`) re-measures, since
 *  a font-size change moves neither its width nor its inline height. */
export const CHAT_ZOOM_EVENT = "fused-render:chat-zoom";

function apply(next: number): void {
  const changed = next !== scale;
  scale = next;
  if (typeof document === "undefined") return;
  const style = document.documentElement?.style;
  if (!style) return;
  if (next === 1) style.removeProperty(CSS_VAR);
  else style.setProperty(CSS_VAR, String(next));
  if (changed && typeof window !== "undefined") window.dispatchEvent(new Event(CHAT_ZOOM_EVENT));
}

function announce(next: number): void {
  if (typeof BroadcastChannel !== "function") return;
  try {
    // A throwaway channel per post, as api.ts's prefs channel does: nothing is
    // left open, and every other document's listener still hears it.
    const ch = new BroadcastChannel(CHANNEL);
    ch.postMessage({ chatTextScale: next });
    ch.close();
  } catch {
    // Refused: other documents catch up on their next load.
  }
}

/** Set the factor for every chat: this document now, the server doc (debounced),
 *  and every other open document of this origin. */
export function setChatTextScale(next: number): void {
  const value = snapChatTextScale(next);
  if (value === scale) return;
  apply(value);
  // SET, never step relative to `d` (ui-layout.ts's replay rule).
  patchUiLayout((d) => (d.chatTextScale === value ? d : { ...d, chatTextScale: value }));
  announce(value);
}

/** The keydown handler, exported for tests. True when it claimed the press. */
export function handleChatZoomKey(
  e: KeyLike & { target: EventTarget | null; defaultPrevented?: boolean; preventDefault(): void },
): boolean {
  if (e.defaultPrevented) return false;
  const step = chatZoomChord(e);
  if (step === null) return false;
  let target: unknown = e.target;
  const doc = typeof document === "undefined" ? null : document;
  if (!target || (doc && (target === doc.body || target === doc.documentElement))) {
    const p = lastPointer as (Closest & { isConnected?: boolean }) | null;
    target = p && p.isConnected !== false ? p : null;
  }
  if (!isInChat(target)) return false;
  e.preventDefault();
  setChatTextScale(stepChatTextScale(scale, step));
  return true;
}

/** Install once per document (idempotent): apply the saved factor, follow a
 *  late hydrate and other documents, and listen for the chords. */
export function installChatZoom(): void {
  if (installed || typeof document === "undefined") return;
  installed = true;
  apply(snapChatTextScale(getUiLayout().chatTextScale ?? 1));
  // A hydrate that landed after the first render replaces the cache.
  subscribeUiLayout(() => apply(snapChatTextScale(getUiLayout().chatTextScale ?? 1)));
  if (typeof BroadcastChannel === "function") {
    try {
      const ch = new BroadcastChannel(CHANNEL);
      ch.onmessage = (ev: MessageEvent) => {
        const v = (ev.data as { chatTextScale?: unknown } | null)?.chatTextScale;
        if (typeof v !== "number" || !Number.isFinite(v)) return;
        const value = snapChatTextScale(v);
        apply(value);
        // Keep this document's ui-layout cache in step too: its next whole-doc
        // PUT (any other layout change) must not write the old factor back over
        // the window that changed it. No announce — that window already did.
        patchUiLayout((d) => (d.chatTextScale === value ? d : { ...d, chatTextScale: value }));
      };
    } catch {
      // No channel: this document follows other windows on its next load.
    }
  }
  document.addEventListener(
    "pointerdown",
    (e) => {
      lastPointer = e.target;
    },
    true,
  );
  // The click fallback only holds while the reader is still on that chat:
  // focus landing anywhere outside one (Tab, an iframe, another window) ends it,
  // so ⌘+ there is never mistaken for the chat's.
  document.addEventListener(
    "focusin",
    (e) => {
      if (!isInChat(e.target)) lastPointer = null;
    },
    true,
  );
  window.addEventListener("blur", () => {
    lastPointer = null;
  });
  document.addEventListener("keydown", (e) => void handleChatZoomKey(e), true);
}

/** Test seam: reset this module's state. */
export function _resetChatZoomForTest(value = 1): void {
  scale = value;
  lastPointer = null;
}
