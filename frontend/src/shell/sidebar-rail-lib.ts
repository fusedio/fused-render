// What the collapsed rail shows below its nav icons (GlobalSidebar): the
// desk's apps, then the pinned bookmarks — each capped so the rail stays a
// column of doors, not a second sidebar. Pure, so the caps and the bot gate
// are testable without rendering the shell.
import type { Bookmark } from "@platform/lib/bookmarks";
import type { CurrentApp } from "@shell/current-apps-lib";

export const RAIL_APPS_MAX = 10;
export const RAIL_PINS_MAX = 3;

/** Both lists in the order their sections show them, capped. Under Fused Bot
 *  neither section exists, so neither group does. */
export function railExtras({
  apps,
  pins,
  bot,
}: {
  apps: CurrentApp[];
  pins: Bookmark[];
  bot: boolean;
}): { apps: CurrentApp[]; pins: Bookmark[] } {
  if (bot) return { apps: [], pins: [] };
  return { apps: apps.slice(0, RAIL_APPS_MAX), pins: pins.slice(0, RAIL_PINS_MAX) };
}
