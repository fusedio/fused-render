// Icon-only mode switcher for the preview header (SPEC PT-10). Shared by
// TemplatePreview (template registry modes, icons fetched via /api/fs/raw)
// and HtmlPreview (fixed render/source pair, shell-baked inline SVG icons).
// Rendered only when there is more than one entry — a single mode needs no
// switcher, for either caller.
import React from "react";
import { rawUrl } from "../lib/api";
import type { TemplateEntry } from "../lib/api";

export interface ModeSwitcherEntry<M extends string> {
  mode: M;
  icon: React.ReactNode;
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
          title={e.mode}
          onClick={() => onSelect(e.mode)}
        >
          {e.icon}
        </button>
      ))}
    </div>
  );
}

// Icon for a template-registry mode entry (PT-11): a monochrome SVG tinted
// via CSS mask-image + currentColor (so active/inactive coloring is free),
// or — when the resolved template ships no icon.svg — a placeholder box with
// the mode's first letter.
export function templateModeIcon(entry: TemplateEntry): React.ReactNode {
  if (entry.icon === null) {
    return <span className="mode-icon-placeholder">{entry.mode.charAt(0).toUpperCase()}</span>;
  }
  const mask = `url("${rawUrl(entry.icon)}")`;
  return <span className="mode-icon-mask" style={{ WebkitMaskImage: mask, maskImage: mask }} />;
}
