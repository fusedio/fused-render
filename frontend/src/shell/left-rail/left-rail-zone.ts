// THE LEFT SIDEBAR'S COLD AND HOT ZONES (rail-zone-lib header; owner,
// 2026-11-27: the left sidebar's hover cards behave exactly as the Claude
// rail's doors do — collapsed or expanded, "same hot zone cold zone thing").
// Its OWN zone — the sidebar and its own cards and bridges — not the Claude
// rail's: a pointer crossing the left
// sidebar from the page waits RAIL_COLD_OPEN_MS, once it is in the zone every
// icon's card shows at once, and the heat survives RAIL_COOL_MS off it.
// Watched while LeftPeek stands with the switch on and the sidebar drawn.
import { createRailZoneWatch } from "@shell/claude-rail/rail-zone";

/** The left zone: the sidebar — collapsed or expanded, and its width seam —
 *  the left cards and their bridges. */
export const LEFT_ZONE_SELECTOR = "#sidebar, .sidebar-resize-handle, .claude-rail-peek.is-left, .claude-rail-peek-bridge.is-left";

const leftZone = createRailZoneWatch(LEFT_ZONE_SELECTOR);

/** The pace every left card's hover waits on (door-card-store `opts.pace`). */
export const leftPace = leftZone.pace;
export const installLeftRailZone = leftZone.install;
export const inLeftRailZone = leftZone.inZone;
export const leftRailZoneForTests = leftZone.zoneForTests;
export const resetLeftRailZoneForTests = leftZone.resetForTests;
export const setLeftRailZoneClockForTests = leftZone.setClockForTests;
