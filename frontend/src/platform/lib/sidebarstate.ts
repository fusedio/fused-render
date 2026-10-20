// Sidebar chrome state — resizable width + collapsed flag. Persisted so the
// layout the user dragged into place survives reloads AND relaunches: it is the
// `sidebar` field of the server-held layout document (ui-layout.ts, D1386),
// read synchronously off that module's cache. It was the localStorage key
// `fused-render:sidebar` until the desktop app's port walk (a new origin, so
// empty storage, on most relaunches) was found resetting it.
import {
  SIDEBAR_WIDTH_DEFAULT,
  SIDEBAR_WIDTH_MAX,
  SIDEBAR_WIDTH_MIN,
  getUiLayout,
  patchUiLayout,
  subscribeUiLayout,
} from "./ui-layout";

// The document's own bounds (ui-layout.ts, mirrored by the server's validator).
export const SIDEBAR_MIN_WIDTH = SIDEBAR_WIDTH_MIN;
export const SIDEBAR_MAX_WIDTH = SIDEBAR_WIDTH_MAX;
export const SIDEBAR_DEFAULT_WIDTH = SIDEBAR_WIDTH_DEFAULT;

// What the sidebar occupies while COLLAPSED — the icon rail, and not zero: the
// panel is still on screen, wearing a narrower shape. Duplicated as a literal in
// `#sidebar.sidebar-collapsed` (styles/sidebar.css, where the 48 is argued); it
// is here as well because the reopen drag measures its pull from the rail's outer
// edge, and reading a width back out of a stylesheet to do arithmetic on it is
// worse than one number written down twice with a note at each end.
export const SIDEBAR_RAIL_WIDTH = 48;

export interface SidebarState {
  width: number;
  collapsed: boolean;
}

/** The PERSISTED state — the reader's last choice, as the layout document
 *  holds it. Distinct from `getSidebarState()`, the live one: a resize drag and
 *  the task peek's auto-collapse move the sidebar without writing here. */
export function loadSidebarState(): SidebarState {
  const stored = getUiLayout().sidebar;
  if (!stored) return { width: SIDEBAR_DEFAULT_WIDTH, collapsed: false };
  const width = Number.isFinite(stored.width)
    ? Math.min(SIDEBAR_MAX_WIDTH, Math.max(SIDEBAR_MIN_WIDTH, stored.width))
    : SIDEBAR_DEFAULT_WIDTH;
  return { width, collapsed: stored.collapsed === true };
}

export function saveSidebarState(state: SidebarState): void {
  // Rounded and clamped HERE, not trusted to the caller: a drag ends on a
  // fractional pixel, and the server 400s anything but an int in bounds.
  const sidebar = {
    width: Math.round(Math.min(SIDEBAR_MAX_WIDTH, Math.max(SIDEBAR_MIN_WIDTH, state.width))),
    collapsed: state.collapsed,
  };
  patchUiLayout((d) => ({ ...d, sidebar }));
}

// --- Shared live state ------------------------------------------------------
// Width and collapsed flag live here rather than in SidebarFrame's useState:
// the frame is remounted by every sub-app that composes it (explorer, builder,
// preferences), and a per-mount useState would reload the persisted value on
// each route change — losing an in-session drag and letting two mounts hold
// different widths mid-transition. One store, one layout.
let current: SidebarState | null = null;
// What `current` was seeded from, by identity — so a late layout hydrate (one
// that missed the boot's 2 s bound, ui-layout.ts) can tell "still showing the
// defaults it seeded" from "moved since, by a drag or the peek's auto-collapse",
// and re-seed only the former.
let seeded: SidebarState | null = null;
const listeners = new Set<() => void>();

export function getSidebarState(): SidebarState {
  if (current === null) current = seeded = loadSidebarState();
  return current;
}

subscribeUiLayout(() => {
  if (current === null || current !== seeded) return;
  // Any field of the document notifies; only a sidebar change re-seeds.
  const fresh = loadSidebarState();
  if (fresh.width === seeded.width && fresh.collapsed === seeded.collapsed) return;
  current = seeded = fresh;
  listeners.forEach((fn) => fn());
});

// `persist` is false for the per-pointermove width updates of a resize drag —
// the final width is written once at drag end.
export function setSidebarState(
  next: SidebarState | ((s: SidebarState) => SidebarState),
  persist = true
): void {
  const prev = getSidebarState();
  const value = typeof next === "function" ? next(prev) : next;
  if (value === prev) return;
  current = value;
  if (persist) saveSidebarState(value);
  listeners.forEach((fn) => fn());
}

export function toggleSidebarCollapsed(): void {
  setSidebarState((s) => ({ ...s, collapsed: !s.collapsed }));
}

export function subscribeSidebarState(fn: () => void): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}
