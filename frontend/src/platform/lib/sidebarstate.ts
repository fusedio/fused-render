// Sidebar chrome state — resizable width + collapsed flag. Persisted so the
// layout the user dragged into place survives reloads. Same defensive
// localStorage pattern as viewstate.ts: best-effort, silent on failure.
const KEY = "fused-render:sidebar";

export const SIDEBAR_MIN_WIDTH = 180;
export const SIDEBAR_MAX_WIDTH = 400;
export const SIDEBAR_DEFAULT_WIDTH = 232;

// What the sidebar occupies while COLLAPSED — the icon rail, and not zero: the
// panel is still on screen, wearing a narrower shape. Duplicated as a literal in
// `#sidebar.sidebar-collapsed` (styles/sidebar.css, where the 44 is argued); it
// is here as well because the reopen drag measures its pull from the rail's outer
// edge, and reading a width back out of a stylesheet to do arithmetic on it is
// worse than one number written down twice with a note at each end.
export const SIDEBAR_RAIL_WIDTH = 44;

export interface SidebarState {
  width: number;
  collapsed: boolean;
}

export function loadSidebarState(): SidebarState {
  const fallback: SidebarState = { width: SIDEBAR_DEFAULT_WIDTH, collapsed: false };
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return fallback;
    const parsed = JSON.parse(raw) as Partial<SidebarState>;
    const width =
      typeof parsed.width === "number" && Number.isFinite(parsed.width)
        ? Math.min(SIDEBAR_MAX_WIDTH, Math.max(SIDEBAR_MIN_WIDTH, parsed.width))
        : SIDEBAR_DEFAULT_WIDTH;
    return { width, collapsed: parsed.collapsed === true };
  } catch {
    return fallback; // private-mode / quota / malformed JSON — behave as default
  }
}

export function saveSidebarState(state: SidebarState): void {
  try {
    localStorage.setItem(KEY, JSON.stringify(state));
  } catch {
    // storage unavailable — state is best-effort, so a failed write is fine
  }
}

// --- Shared live state ------------------------------------------------------
// Width and collapsed flag live here rather than in SidebarFrame's useState:
// the frame is remounted by every sub-app that composes it (explorer, builder,
// preferences), and a per-mount useState would reload the persisted value on
// each route change — losing an in-session drag and letting two mounts hold
// different widths mid-transition. One store, one layout.
let current: SidebarState | null = null;
const listeners = new Set<() => void>();

export function getSidebarState(): SidebarState {
  if (current === null) current = loadSidebarState();
  return current;
}

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
