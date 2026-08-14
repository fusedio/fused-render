// Template-mode ICONS and naming, shared by every mode surface (SPEC PT-10/
// PT-11). Real modes get icons fetched via /api/fs/raw; the sentinels get a
// shell-baked inline SVG (no folder to ship icon.svg from).
//
// The icon-strip switcher this module used to export is gone: the topbar, the
// listing preview-pane header and the pane bars all render the one shared
// dropdown (BarMenu's ModeMenu) instead, so the strip had no call-sites left.
import React from "react";
import { rawUrl } from "@platform/lib/api";
import type { TemplateEntry } from "@platform/lib/api";

// Sentinel modes the shell renders without a template folder (SPEC PT-12/D81):
// `_render` (the target file itself, in an iframe) and `_listing` (the shell's
// built-in directory listing, no iframe). Any other `path: null` entry is an
// unknown sentinel — filtered out by every view that dispatches on templates
// (Preview, PaneModeMenu), so they share this one set to stay in lockstep.
export const KNOWN_SENTINEL_MODES = new Set(["_render", "_listing"]);

// Display name for a mode key. The implementation moved to
// platform/lib/mode-name.ts (pure, unit-tested) once the shared ModeMenu
// started SHOWING the name rather than only tooltipping it; re-exported here
// because every mode surface already imports from this module.
export { modeTitle } from "@platform/lib/mode-name";

// Shell-baked icon for the "_render" sentinel (PT-12) — sentinels have no
// template folder, so there's no icon.svg to fetch. Component-local; the same
// play-in-a-rounded-box glyph as the folder pane's Preview side
// (PREVIEW_SIDE_ICON, SideChrome.tsx), because the two are the same "look at
// the rendered thing" idea and used to wear different icons (an eye here).
const RENDER_SENTINEL_ICON = (
  <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <rect x="3" y="3" width="18" height="18" rx="3" />
    <path d="M10 8.75 15.5 12 10 15.25Z" />
  </svg>
);

// Shell-baked icon for the "_listing" sentinel (PT-12/D81) — the built-in
// directory listing; sentinels have no template folder to ship icon.svg.
const LISTING_SENTINEL_ICON = (
  <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <line x1="8" y1="6" x2="21" y2="6" />
    <line x1="8" y1="12" x2="21" y2="12" />
    <line x1="8" y1="18" x2="21" y2="18" />
    <line x1="3" y1="6" x2="3.01" y2="6" />
    <line x1="3" y1="12" x2="3.01" y2="12" />
    <line x1="3" y1="18" x2="3.01" y2="18" />
  </svg>
);

// Icon for a template-mode entry (PT-11): a sentinel mode gets a shell-baked
// SVG; a resolved template with no icon.svg gets a placeholder box with the
// mode's first letter; otherwise a monochrome SVG tinted via CSS mask-image +
// currentColor (so active/inactive coloring is free).
export function templateModeIcon(entry: TemplateEntry): React.ReactNode {
  if (entry.mode === "_render") {
    return RENDER_SENTINEL_ICON;
  }
  if (entry.mode === "_listing") {
    return LISTING_SENTINEL_ICON;
  }
  if (entry.icon === null) {
    return <span className="mode-icon-placeholder">{entry.mode.charAt(0).toUpperCase()}</span>;
  }
  const mask = `url("${rawUrl(entry.icon)}")`;
  return <span className="mode-icon-mask" style={{ WebkitMaskImage: mask, maskImage: mask }} />;
}
