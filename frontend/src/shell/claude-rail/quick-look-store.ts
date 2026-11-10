// QUICK LOOK, THE HOLD (owner, 2026-11-10; design.md "Quick look v2"): Shift
// held. Nothing else lives here — the preview itself is each window's own
// (rail-peek-lib `PreviewIntent`, a transient key beside the pinned one):
// window A's in ClaudeRail, window B's in fav-peek-store. Those register here
// (`registerPreview`) so that letting go of Shift, and Escape (quick-look-
// keys), send every window back to its pinned chat at once.
//
// v1 (a floating card over everything, on Space) is gone: "shouldn't open
// another window, show in the same window … Space messes my typing".
import { useSyncExternalStore } from "react";

/** A window's preview, as the hold sees it. */
export interface QuickLookPreview {
  readonly current: string | null;
  clear(): void;
}

let held = false;
const subs = new Set<() => void>();
const previews = new Set<QuickLookPreview>();

function tell(): void {
  for (const fn of [...subs]) fn();
}

/** Shift went down (quick-look-keys). Nothing shows yet. */
export function holdQuickLook(): void {
  if (held) return;
  held = true;
  tell();
}

/** Shift came up (or its keyup was lost: a blur, a hidden page, a key event
 *  without Shift): every window goes back to its pinned chat. */
export function releaseQuickLook(): void {
  revertPreviews();
  if (!held) return;
  held = false;
  tell();
}

export function quickLookHeld(): boolean {
  return held;
}

export function subscribeQuickLook(fn: () => void): () => void {
  subs.add(fn);
  return () => {
    subs.delete(fn);
  };
}

/** Whether Shift is held, re-rendering on each change. */
export function useQuickLookHeld(): boolean {
  return useSyncExternalStore(subscribeQuickLook, quickLookHeld, quickLookHeld);
}

/** A window's preview machine, for the release and Escape to reach. */
export function registerPreview(p: QuickLookPreview): () => void {
  previews.add(p);
  return () => {
    previews.delete(p);
  };
}

/** Every window back to its pinned chat (Escape, the release). True when one
 *  was previewing. */
export function revertPreviews(): boolean {
  let any = false;
  for (const p of [...previews]) {
    if (p.current !== null) any = true;
    p.clear();
  }
  return any;
}

export function resetQuickLookForTests(): void {
  held = false;
  subs.clear();
  previews.clear();
}
