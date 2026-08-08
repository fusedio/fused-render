// Sidebar chrome state — resizable width + collapsed flag. Persisted so the
// layout the user dragged into place survives reloads. Same defensive
// localStorage pattern as viewstate.ts: best-effort, silent on failure.
const KEY = "fused-render:sidebar";

export const SIDEBAR_MIN_WIDTH = 180;
export const SIDEBAR_MAX_WIDTH = 400;
export const SIDEBAR_DEFAULT_WIDTH = 232;

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
// The collapsed flag is no longer the sidebar's private business: with the
// sidebar collapsed, the control that brings it back lives in the EXPLORER
// TOPBAR (a floating bubble hanging off a 10px strip was half off-screen and
// crowded the bookmark star). Two sibling subtrees therefore read and write
// the same flag, so it lives in this module-level store rather than in
// SidebarFrame's useState.
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
