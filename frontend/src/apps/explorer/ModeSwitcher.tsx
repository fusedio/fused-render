// Icon-only mode switcher for the preview header (SPEC PT-10), used by
// TemplatePreview for every template-mode list, including the hardcoded
// html `["_render", "code"]` pair (PT-12) — real modes get icons fetched via
// /api/fs/raw, the "_render" sentinel gets a shell-baked inline SVG (no
// folder to ship icon.svg from). Rendered only when there is more than one
// entry — a single mode needs no switcher.
import React from "react";
import { rawUrl } from "@platform/lib/api";
import type { TemplateEntry } from "@platform/lib/api";

// Sentinel modes the shell renders without a template folder (SPEC PT-12/D81):
// `_render` (the target file itself, in an iframe) and `_listing` (the shell's
// built-in directory listing, no iframe). Any other `path: null` entry is an
// unknown sentinel — filtered out by every view that dispatches on templates
// (Preview, PaneModeMenu), so they share this one set to stay in lockstep.
export const KNOWN_SENTINEL_MODES = new Set(["_render", "_listing"]);

export interface ModeSwitcherEntry<M extends string> {
  mode: M;
  icon: React.ReactNode;
  // Condition.py gate not yet resolved (CT-12): rendered as a disabled
  // spinner until the background /api/fs/conditions verdict lands.
  pending?: boolean;
}

// Modes whose FOLDER NAME is not a label a person should read. A template's
// folder name is its identity (SPEC §0) and is chosen for the filesystem, not
// for a tooltip — capitalizing `claude_split` yields "Claude_split", which was a
// blemish while that mode showed on app folders alone and is now the switcher's
// most-seen label, on every file key it gained (D230). So the few names that
// read badly, or that name an implementation where the user sees a feature, get
// a display name here; everything else stays capitalized, because a per-template
// naming registry is a thing to maintain and most folder names are already right.
// `claude` is deliberately NOT also "Chat": an app folder passes both chat gates
// (the `/` key carries `claude_split` and `claude`, and the builder's APP_MODES
// pin is what hides the second one THERE, not in the explorer), so two entries
// labelled "Chat" would sit side by side on exactly the folder where the
// difference matters. "Folder chat" is what `claude` actually is now.
const MODE_TITLES: Record<string, string> = {
  claude_split: "Chat",
  claude: "Folder chat",
  versions: "History",
  git: "Source Control",
};

// Human-readable tooltip for a mode name: the "_render" sentinel reads as
// "Rendered", a name in MODE_TITLES reads as its display name, and any other
// mode name is capitalized ("code" → "Code").
// Exported for PaneModeMenu (pane/tab chrome shares the naming).
export function modeTitle(mode: string): string {
  if (mode === "_render") return "Rendered";
  if (mode === "_listing") return "Listing";
  if (mode === "_app") return "App"; // pane-only sentinel (ListingPreviewPane)
  const titled = MODE_TITLES[mode];
  if (titled) return titled;
  return mode.charAt(0).toUpperCase() + mode.slice(1);
}

interface ModeSwitcherProps<M extends string> {
  entries: ModeSwitcherEntry<M>[];
  active: M;
  // The mode a click is currently switching TO, if any. The switch is async
  // (the preview asks the open editor to flush its buffer first, bounded at
  // 10s) and clicks landing mid-switch are dropped, so the entry that was
  // clicked shows a spinner until the iframe swap starts — otherwise a slow
  // switch reads as a dead button.
  busy?: M | null;
  onSelect: (mode: M) => void;
}

export default function ModeSwitcher<M extends string>({ entries, active, busy, onSelect }: ModeSwitcherProps<M>) {
  if (entries.length <= 1) return null;
  return (
    <div className="mode-switcher">
      {entries.map((e) => {
        const waiting = busy === e.mode;
        return (
          <button
            key={e.mode}
            type="button"
            className={
              "mode-switcher-btn" +
              (e.mode === active ? " active" : "") +
              (e.pending ? " pending" : "") +
              (waiting ? " switching" : "")
            }
            title={
              waiting
                ? `${modeTitle(e.mode)} — switching…`
                : e.pending
                  ? `${modeTitle(e.mode)} — checking if this view applies…`
                  : modeTitle(e.mode)
            }
            disabled={e.pending || waiting}
            onClick={() => onSelect(e.mode)}
          >
            {e.pending || waiting ? <span className="mode-icon-spinner" /> : e.icon}
          </button>
        );
      })}
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
