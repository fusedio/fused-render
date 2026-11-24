// THE CLAUDE RAIL'S GUTTER (owner, 2026-11-05: "move the notification modal
// to the right a little bit so it doesn't cover the mini sidebar … basically
// all menus will shift to right a little bit"): how far a surface on the
// window's right edge must keep off it. The rail says its width on the
// document while it stands (shell/claude-rail ClaudeRail `railUp` sets
// `--claude-rail-w`); nothing stands, 0. Read, not imported: the platform
// may not import the shell, and a CSS custom property is the one contract
// both sides can hold.

const PROP = "--claude-rail-w";

/** The rail's width now, in px (0 without one, or outside a document). */
export function railGutter(): number {
  if (typeof document === "undefined" || !document.documentElement) return 0;
  const raw = getComputedStyle(document.documentElement).getPropertyValue(PROP).trim();
  const n = parseFloat(raw);
  return Number.isFinite(n) && n > 0 ? n : 0;
}

/** A positioner's collision padding (Base UI `collisionPadding`): its 5px on
 *  every side, plus the rail on the right — a menu that would open under the
 *  rail flips or slides left of it instead. */
export function railCollisionPadding(): { top: number; right: number; bottom: number; left: number } {
  return { top: 5, right: 5 + railGutter(), bottom: 5, left: 5 };
}
