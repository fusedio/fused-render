// What the collapsed rail shows below its nav icons (GlobalSidebar): the
// desk's apps, then its canvases, then the pinned bookmarks — each capped so
// the rail stays a column of doors, not a second sidebar. Pure, so the caps,
// the split and the gates are testable without rendering the shell.
import type { Bookmark } from "@platform/lib/bookmarks";
import type { CurrentApp } from "@shell/current-apps-lib";

// Apps + canvases sum to the ten the apps group had alone (owner,
// 2026-10-22: "reduce the number of apps being shown, and use the freed up
// space to show canvases") — the rail's height does not grow.
export const RAIL_APPS_MAX = 6;
export const RAIL_CANVASES_MAX = 4;
export const RAIL_PINS_MAX = 3;

/** The three groups in the order their sections show them, capped. `desk` is
 *  the whole desk in section order; it splits on `canvas` exactly as the
 *  sections do (CurrentAppsSection `inSection`). Under Fused Bot no section
 *  exists, so no group does; the canvases group follows the Canvases
 *  section's own gate (`canvases`: the feature on), since a door to
 *  a clone with no account behind it opens a page that does not work. */
export function railExtras({
  desk,
  pins,
  bot,
  canvases,
}: {
  desk: CurrentApp[];
  pins: Bookmark[];
  bot: boolean;
  canvases: boolean;
}): { apps: CurrentApp[]; canvases: CurrentApp[]; pins: Bookmark[] } {
  if (bot) return { apps: [], canvases: [], pins: [] };
  return {
    apps: desk.filter((a) => a.canvas === null).slice(0, RAIL_APPS_MAX),
    canvases: canvases ? desk.filter((a) => a.canvas !== null).slice(0, RAIL_CANVASES_MAX) : [],
    pins: pins.slice(0, RAIL_PINS_MAX),
  };
}
