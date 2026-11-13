// What the collapsed rail shows below its nav icons (GlobalSidebar): the
// desk's apps, then its canvases, then the pinned bookmarks — each capped so
// the rail stays a column of doors, not a second sidebar. Pure, so the caps,
// the split and the gates are testable without rendering the shell.
import type { Bookmark } from "@platform/lib/bookmarks";
import type { CurrentApp } from "@shell/current-apps-lib";

// Seven of each (owner, 2026-11-09: was 5 / 5 / 3); the rail's middle
// scrolls on a short window (sidebar.css `.sidebar-rail-scroll`).
export const RAIL_APPS_MAX = 7;
export const RAIL_CANVASES_MAX = 7;
export const RAIL_PINS_MAX = 7;

/** The three groups in the order their sections show them, capped. `desk` is
 *  the whole desk in section order; it splits on `canvas` exactly as the
 *  sections do (CurrentAppsSection `inSection`). The canvases group follows
 *  the Canvases section's own gate (`canvases`: the feature on, owner 2026-10-22 — no
 *  sign-in half, so a new machine sees the group with the switch). */
export function railExtras({
  desk,
  pins,
  canvases,
}: {
  desk: CurrentApp[];
  pins: Bookmark[];
  canvases: boolean;
}): { apps: CurrentApp[]; canvases: CurrentApp[]; pins: Bookmark[] } {
  return {
    apps: desk.filter((a) => a.canvas === null).slice(0, RAIL_APPS_MAX),
    canvases: canvases ? desk.filter((a) => a.canvas !== null).slice(0, RAIL_CANVASES_MAX) : [],
    pins: pins.slice(0, RAIL_PINS_MAX),
  };
}
