// QUICK LOOK IN PLACE, the rulings without a DOM (owner, 2026-11-10;
// design.md "Quick look v2"; review of c7270b2ab): which window a ring may
// preview into, what a held Shift over it does, and how the window's preview
// key is written — so ClaudeRail and FavouritePeek decide the same way and a
// test can say so. The machine itself is rail-peek-lib `PreviewIntent`.
import type { RailRow } from "./claude-rail-lib";
import { keepAlivePinned, type PreviewSink } from "./rail-peek-lib";

/** THE ONE PREDICATE: a window previews only while it SHOWS a PINNED CHAT
 *  card — the seat it draws (`shown`, exactly what goes to RailPeek), pinned
 *  (window A's press; window B's open favourite), with a conversation. A
 *  hover's card, a seat with no chat, no card drawn: no preview, the hover
 *  rules of before untouched — no ring lit, no hover swallowed, no Escape
 *  taken. */
export function previewable(o: { shown: RailRow | null; pinned: boolean }): boolean {
  return o.pinned && !!o.shown?.pulse.session_id;
}

export type LookSection = "a" | "b";

/** What a held Shift over ring `key` of `section` does: aim the window's
 *  preview, send it back to pinned (its own pinned ring), or nothing (Shift
 *  not held, a ring of no window, a window not previewable, a key no longer
 *  among the section's rings). */
export function lookTarget(o: {
  held: boolean;
  section: LookSection | null;
  looks: boolean;
  live: boolean;
  key: string;
  pinned: string | null;
}): "aim" | "clear" | null {
  if (!o.held || !o.section || !o.looks || !o.live) return null;
  return o.key === o.pinned ? "clear" : "aim";
}

/** The ring the pointer rested on, while its section still draws it. */
export function liveLookRing<T extends { key: string; section: LookSection }>(
  ring: T | null,
  liveA: ReadonlySet<string>,
  liveB: ReadonlySet<string>,
): T | null {
  if (!ring) return null;
  return (ring.section === "a" ? liveA : liveB).has(ring.key) ? ring : null;
}

/** SHIFT PRESSED WITH THE POINTER ALREADY ON A RING aims it — never while a
 *  field has the caret: that Shift is a capital letter, and the preview would
 *  hide the composer being typed in (review, 2026-11-10). */
export function turnedAims(o: { turned: boolean; ring: boolean; editable: boolean }): boolean {
  return o.turned && o.ring && !o.editable;
}

/** Shift+press is a PLAIN press only while a window previews (its commit or
 *  its way back); otherwise Shift is the link's own modifier, as ever. */
export function shiftIsPlain(o: { held: boolean; previewing: boolean }): boolean {
  return o.held && o.previewing;
}

/** WINDOW A'S PREVIEW, WRITTEN (ClaudeRail): the machine's sink touches the
 *  preview key and the kept set — the pinned chat never pushed out of it —
 *  and nothing else; the pinned key is read, never written. */
export function previewSink(o: {
  setPreview: (key: string | null) => void;
  setKept: (fn: (kept: readonly string[]) => readonly string[]) => void;
  pinned: () => string | null;
  onShow?: (key: string | null) => void;
}): PreviewSink {
  return {
    show: (key) => {
      o.onShow?.(key);
      o.setPreview(key);
    },
    warm: (key) => o.setKept((s) => keepAlivePinned(s, key, o.pinned())),
  };
}
