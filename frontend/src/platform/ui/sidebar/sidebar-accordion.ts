// The sidebar's three foldable sections (Projects, Canvases, Favourites) are
// an accordion: at most ONE is open at a time (owner, 2026-10-21). Opening one
// folds the others; folding the open one leaves all three folded.
//
// The fold flags stay the three booleans the layout document already carries
// (`currentAppsCollapsed`, `canvasesCollapsed`, `bookmarksCollapsed`, D1386) —
// no new field, so shell/ui_layout.py's validator is untouched and a document
// written before the accordion still reads. The invariant is enforced on READ
// (the first unfolded section in sidebar order is the open one; the rest show
// folded whatever their raw flag says) and restored on WRITE (every click sets
// all three flags, never toggles one — the ui-layout header's rule).
//
// `patchUiLayout` does not notify subscribers (the store that patched already
// knows), but here three components share the flags, so this module keeps its
// own listener set and fires it after each write. A section that is not
// mounted (Canvases, when the Canvases nav is off) cannot be the open one: the
// hook registers the keys on screen, and the read skips the rest.
import { useEffect, useSyncExternalStore } from "react";
import { getUiLayout, patchUiLayout, subscribeUiLayout, type UiLayoutDoc } from "@platform/lib/ui-layout";

export type SidebarSectionKey = "projects" | "canvases" | "favourites";

/** Sidebar order — the first unfolded one wins. */
const ORDER: SidebarSectionKey[] = ["projects", "canvases", "favourites"];

const FIELD: Record<SidebarSectionKey, "currentAppsCollapsed" | "canvasesCollapsed" | "bookmarksCollapsed"> = {
  projects: "currentAppsCollapsed",
  canvases: "canvasesCollapsed",
  favourites: "bookmarksCollapsed",
};

const listeners = new Set<() => void>();
function notify(): void {
  listeners.forEach((fn) => fn());
}

const mounted = new Set<SidebarSectionKey>();

/** The one section showing its rows, or null when all are folded. */
export function openSidebarSection(doc: UiLayoutDoc = getUiLayout()): SidebarSectionKey | null {
  for (const key of ORDER) {
    if (mounted.size && !mounted.has(key)) continue;
    if (doc[FIELD[key]] !== true) return key;
  }
  return null;
}

/** Unfold `key` (folding the others) or fold it (leaving all folded). */
export function setSidebarSectionOpen(key: SidebarSectionKey, open: boolean): void {
  patchUiLayout((d) => {
    let next = d;
    for (const k of ORDER) {
      const folded = !(open && k === key);
      if (next[FIELD[k]] !== folded) next = { ...next, [FIELD[k]]: folded };
    }
    return next;
  });
  notify();
}

function subscribe(fn: () => void): () => void {
  listeners.add(fn);
  const off = subscribeUiLayout(fn);
  return () => {
    listeners.delete(fn);
    off();
  };
}

/** Whether `key` is the open section. Re-renders when any section's click
 *  changes the answer, or a late hydrate replaces the document. */
export function useSidebarSectionOpen(key: SidebarSectionKey): boolean {
  useEffect(() => {
    mounted.add(key);
    notify();
    return () => {
      mounted.delete(key);
      notify();
    };
  }, [key]);
  return useSyncExternalStore(subscribe, () => openSidebarSection() === key);
}
