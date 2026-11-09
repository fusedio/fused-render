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
// layout document (ui-layout.ts, D1386), read synchronously off that module's
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

/**
 * FULL SCREEN, ASKED FOR FROM OUTSIDE THE PANE (a rail card's ⋮ → Full screen,
 * 2026-11-09). The card hops to the chat's page; setting the flag straight
 * after the hop raced the new page's own guards — the pane mounts on its
 * preview tab first, "leaving the chat tab leaves full screen" clears the
 * flag, and the chat opened in a plain sidebar. So the card leaves a ONE-SHOT
 * request naming the session, and the side-pane chat for that session takes
 * it once it is mounted there (ClaudeChat). Short-lived: a request nobody
 * takes must not maximize some later chat.
 */
const REQUEST_TTL_MS = 15_000;
let request: { sessionId: string; at: number } | null = null;
const requestListeners = new Set<() => void>();

export function requestSideMaximized(sessionId: string): void {
  if (!sessionId) return;
  request = { sessionId, at: Date.now() };
  requestListeners.forEach((fn) => fn());
}

/** Drop a request whose hop was refused (a leave guard said no). */
export function cancelSideMaximizedRequest(sessionId: string): void {
  if (request?.sessionId === sessionId) request = null;
}

/** True once, for the chat the request names, while it is fresh. */
export function takeSideMaximizedRequest(sessionId: string): boolean {
  if (!request || !sessionId) return false;
  if (Date.now() - request.at > REQUEST_TTL_MS) {
    request = null;
    return false;
  }
  if (request.sessionId !== sessionId) return false;
  request = null;
  return true;
}

export function subscribeSideMaximizedRequest(fn: () => void): () => void {
  requestListeners.add(fn);
  return () => {
    requestListeners.delete(fn);
  };
}

/**
 * WHETHER A CHAT'S DOOR LANDS IN A SIDE PANE that can go full screen. A canvas
 * chat's lands on the canvas page (`/canvases/<env>/<name>`), whose chat pane
 * has no full screen — its own ⋮ offers none — so a card's Full screen there
 * would only open a plain sidebar (Akshil, 2026-11-09).
 */
export function opensInSidePane(href: string): boolean {
  let path = href;
  try {
    path = new URL(href, "http://x").pathname;
  } catch {
    /* a bare path is read as it stands */
  }
  return !path.startsWith("/canvases/");
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
