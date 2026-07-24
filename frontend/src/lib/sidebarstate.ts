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
