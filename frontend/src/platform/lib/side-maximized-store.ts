// Whether the companion side pane (Claude / Git) is expanded to FILL THE WINDOW
// ("full screen" / focus mode). One boolean, shared by BOTH surfaces that have a
// side pane — the file preview's sidebar (PreviewSidebar) and the folder
// listing's pane (ListingPreviewPane) — so the choice survives a hop between
// them. Sibling to `side-hidden-store.ts` (shut/open) and `side-store.ts` (width).
//
// The pane is not remounted to go full screen: it stays where it is in the DOM
// and a `.maximized` class makes it `position: fixed; inset: 0` (preview.css), so
// the chat inside keeps its state and the content column underneath stays mounted.
//
// Persisted in `localStorage`, like its siblings, and every access is wrapped: a
// private window or blocked storage makes the accessor itself throw, and a
// failure costs the persistence, never the flag — the module variable is the
// live answer. Unlike the siblings it has subscribers (and a hook), because the
// header button, the Esc handler and the close path all write it while mounted
// components read it.
import { useEffect, useSyncExternalStore } from "react";

/** Stored as "1" while maximized; the key is absent otherwise. */
export const SIDE_MAXIMIZED_KEY = "fused-render:explorer-side-maximized";

function readStored(): boolean {
  try {
    return localStorage.getItem(SIDE_MAXIMIZED_KEY) === "1";
  } catch {
    return false;
  }
}

function writeStored(next: boolean): void {
  try {
    if (next) localStorage.setItem(SIDE_MAXIMIZED_KEY, "1");
    else localStorage.removeItem(SIDE_MAXIMIZED_KEY);
  } catch {
    // Costs the persistence, never the flag.
  }
}

let maximized = readStored();
const listeners = new Set<() => void>();

export function getSideMaximized(): boolean {
  return maximized;
}

export function setSideMaximized(next: boolean): void {
  if (maximized === next) return;
  maximized = next;
  writeStored(next);
  listeners.forEach((fn) => fn());
}

export function subscribeSideMaximized(fn: () => void): () => void {
  listeners.add(fn);
  return () => {
    listeners.delete(fn);
  };
}

export function useSideMaximized(): boolean {
  return useSyncExternalStore(subscribeSideMaximized, getSideMaximized, getSideMaximized);
}

/** Esc leaves full screen. Registered only while maximized, in the BUBBLE phase,
 *  so the composer's own Esc (stop / cancel) can `preventDefault` first and keeps
 *  priority; and never while a modal dialog is up, where Esc belongs to the dialog. */
export function useSideMaximizedEscape(active: boolean): void {
  useEffect(() => {
    if (!active) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape" || e.defaultPrevented) return;
      if (document.querySelector(".modal-dialog")) return;
      setSideMaximized(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [active]);
}
