// Right-rail chrome state — expanded flag + dragged width. The rail's twin of
// sidebarstate.ts: persisted as the `rail` field of the server-held layout
// document (ui-layout.ts), read synchronously off that module's cache. Absent
// field = collapsed at the default width.
import {
  RAIL_WIDTH_DEFAULT,
  RAIL_WIDTH_MAX,
  RAIL_WIDTH_MIN,
  getUiLayout,
  patchUiLayout,
  subscribeUiLayout,
} from "./ui-layout";

// The document's own bounds (ui-layout.ts, mirrored by the server's validator).
export const RAIL_MIN_WIDTH = RAIL_WIDTH_MIN;
export const RAIL_MAX_WIDTH = RAIL_WIDTH_MAX;
export const RAIL_DEFAULT_WIDTH = RAIL_WIDTH_DEFAULT;

/** What the rail occupies while COLLAPSED (the icon column). */
export const RAIL_COLLAPSED_WIDTH = 48;

export interface RailState {
  width: number;
  expanded: boolean;
}

/** The PERSISTED state, as the layout document holds it. Distinct from
 *  `getRailState()`, the live one: a resize drag moves the rail without
 *  writing here. */
export function loadRailState(): RailState {
  const stored = getUiLayout().rail;
  if (!stored) return { width: RAIL_DEFAULT_WIDTH, expanded: false };
  const width = Number.isFinite(stored.width)
    ? Math.min(RAIL_MAX_WIDTH, Math.max(RAIL_MIN_WIDTH, stored.width))
    : RAIL_DEFAULT_WIDTH;
  return { width, expanded: stored.expanded === true };
}

export function saveRailState(state: RailState): void {
  // Rounded and clamped HERE: the server 400s anything but an int in bounds.
  const rail = {
    width: Math.round(Math.min(RAIL_MAX_WIDTH, Math.max(RAIL_MIN_WIDTH, state.width))),
    expanded: state.expanded,
  };
  patchUiLayout((d) => ({ ...d, rail }));
}

// --- Shared live state ------------------------------------------------------
let current: RailState | null = null;
// What `current` was seeded from, by identity: a late layout hydrate or another
// window's write re-seeds only a store that has not moved since.
let seeded: RailState | null = null;
const listeners = new Set<() => void>();

export function getRailState(): RailState {
  if (current === null) current = seeded = loadRailState();
  return current;
}

subscribeUiLayout(() => {
  if (current === null || current !== seeded) return;
  const fresh = loadRailState();
  if (fresh.width === seeded.width && fresh.expanded === seeded.expanded) return;
  current = seeded = fresh;
  listeners.forEach((fn) => fn());
});

// `persist` is false for the per-pointermove width updates of a resize drag —
// the final width is written once at drag end.
export function setRailState(next: RailState | ((s: RailState) => RailState), persist = true): void {
  const prev = getRailState();
  const value = typeof next === "function" ? next(prev) : next;
  if (value === prev) return;
  current = value;
  if (persist) saveRailState(value);
  listeners.forEach((fn) => fn());
}

export function toggleRailExpanded(): void {
  setRailState((s) => ({ ...s, expanded: !s.expanded }));
}

export function subscribeRailState(fn: () => void): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}
