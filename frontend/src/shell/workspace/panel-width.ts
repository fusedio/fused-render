// THE RIGHT PANEL'S WIDTH (SPEC §56 WS-17), per viewer: the layout document's
// `workspace.rightWidth` (platform/lib/ui-layout, shell/ui_layout.py), the
// railstate.ts pattern — a live value in memory that a drag moves on every
// pointermove, written to the document once, when the drag completes.
import { useSyncExternalStore } from "react";
import { getUiLayout, patchUiLayout, subscribeUiLayout, WS_PANEL_WIDTH_MAX, WS_PANEL_WIDTH_MIN } from "@platform/lib/ui-layout";

/** The panel's floor (the explorer pane's `MIN_W`) and the width it opens at
 *  when nothing was dragged. */
export const PANEL_MIN_W = WS_PANEL_WIDTH_MIN;
export const PANEL_DEFAULT_W = 480;

function stored(): number {
  const w = getUiLayout().workspace?.rightWidth;
  return typeof w === "number" && Number.isFinite(w) ? Math.max(PANEL_MIN_W, Math.min(WS_PANEL_WIDTH_MAX, w)) : PANEL_DEFAULT_W;
}

let live: number | null = null;
let seeded: number | null = null;
const listeners = new Set<() => void>();

export function getPanelWidth(): number {
  if (live === null) live = seeded = stored();
  return live;
}

// A late layout hydrate (or another window's write) re-seeds only a width
// nobody has moved since.
subscribeUiLayout(() => {
  if (live === null || live !== seeded) return;
  const fresh = stored();
  if (fresh === live) return;
  live = seeded = fresh;
  listeners.forEach((fn) => fn());
});

/** Move the live width; `persist` once the drag completes. */
export function setPanelWidth(px: number, persist = false): void {
  const w = Math.round(Math.max(PANEL_MIN_W, Math.min(WS_PANEL_WIDTH_MAX, px)));
  if (w !== live) {
    live = w;
    listeners.forEach((fn) => fn());
  }
  if (persist) {
    seeded = w;
    patchUiLayout((d) => ({ ...d, workspace: { rightWidth: w } }));
  }
}

function subscribe(fn: () => void): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

export function usePanelWidth(): number {
  return useSyncExternalStore(subscribe, getPanelWidth, getPanelWidth);
}
