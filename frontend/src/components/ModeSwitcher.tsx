// Icon-only mode switcher for the preview header (SPEC PT-10), used by
// TemplatePreview for every template-mode list, including the hardcoded
// html `["_render", "code"]` pair (PT-12) — real modes get icons fetched via
// /api/fs/raw, the "_render" sentinel gets a shell-baked inline SVG (no
// folder to ship icon.svg from). Rendered only when there is more than one
// entry — a single mode needs no switcher.
import React from "react";
import { rawUrl } from "../lib/api";
import type { TemplateEntry } from "../lib/api";

export interface ModeSwitcherEntry<M extends string> {
  mode: M;
  icon: React.ReactNode;
}

// Human-readable tooltip for a mode name: the "_render" sentinel reads as
// "Rendered", ordinary mode names are capitalized ("code" → "Code").
// Exported for PaneModeMenu (pane/tab chrome shares the naming).
export function modeTitle(mode: string): string {
  if (mode === "_render") return "Rendered";
  if (mode === "_listing") return "Listing";
  return mode.charAt(0).toUpperCase() + mode.slice(1);
}

interface ModeSwitcherProps<M extends string> {
  entries: ModeSwitcherEntry<M>[];
  active: M;
  onSelect: (mode: M) => void;
}

export default function ModeSwitcher<M extends string>({ entries, active, onSelect }: ModeSwitcherProps<M>) {
  if (entries.length <= 1) return null;
  return (
    <div className="mode-switcher">
      {entries.map((e) => (
        <button
          key={e.mode}
          type="button"
          className={"mode-switcher-btn" + (e.mode === active ? " active" : "")}
          title={modeTitle(e.mode)}
          onClick={() => onSelect(e.mode)}
        >
          {e.icon}
        </button>
      ))}
    </div>
  );
}

// Shell-baked icon for the "_render" sentinel (PT-12) — sentinels have no
// template folder, so there's no icon.svg to fetch. Component-local, matches
// the old hardcoded Rendered|Source eye glyph.
const RENDER_SENTINEL_ICON = (
  <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7-11-7-11-7z" />
    <circle cx="12" cy="12" r="3" />
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
