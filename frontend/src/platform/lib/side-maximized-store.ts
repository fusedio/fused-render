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
// Persisted like its siblings: `explorerSide.maximized` in the server-held
// layout document (ui-layout.ts, D1384), read synchronously off that module's
// cache. It was the localStorage key `fused-render:explorer-side-maximized`
// until the desktop app's port walk (a new origin, so empty storage, on most
// relaunches) was found resetting it; ui-layout.ts imports that key once.
// Unlike the siblings it has subscribers (and a hook), because the header
// button, the Esc handler and the close path all write it while mounted
// components read it.
import { useEffect, useSyncExternalStore } from "react";
import { getUiLayout, patchUiLayout, subscribeUiLayout } from "./ui-layout";

const listeners = new Set<() => void>();

export function getSideMaximized(): boolean {
  return getUiLayout().explorerSide?.maximized === true;
}

export function setSideMaximized(next: boolean): void {
  if (getSideMaximized() === next) return;
  patchUiLayout((d) => ({ ...d, explorerSide: { ...d.explorerSide, maximized: next } }));
  listeners.forEach((fn) => fn());
}

// A layout hydrate that landed after the first render (ui-layout.ts) may have
// changed the answer; tell the hook. Spurious calls are free —
// useSyncExternalStore re-renders only on a changed snapshot.
subscribeUiLayout(() => listeners.forEach((fn) => fn()));

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
