// Inline monochrome icons for the file-explorer context menu
// (components/ContextMenu.tsx, wired up in views/Listing.tsx). Same house
// style as FileIcons/SplitIcons but tuned to match macOS Finder's menu icons:
// 16x16, viewBox 0 0 24 24, fill none, stroke currentColor at a lighter 1.5px
// weight, round caps/joins. Hand-written Lucide-geometry paths — no npm
// dependency. The glyphs are colourless and inherit the row's colour via
// currentColor (so a danger row tints its icon red for free).
import type { ReactNode } from "react";
import { FinderIcon } from "./FinderIcon";

const svgProps = {
  width: 16,
  height: 16,
  viewBox: "0 0 24 24",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 1.5,
  strokeLinecap: "round",
  strokeLinejoin: "round",
  "aria-hidden": true,
} as const;

// One entry per menu action. Kept as ready-made elements (not components) so
// callers just drop `MenuIcons.copy` into an item's `icon` slot.
export const MenuIcons: Record<string, ReactNode> = {
  // Open — arrow pointing up-and-out of a box.
  open: (
    <svg {...svgProps}>
      <path d="M9 5H6a2 2 0 0 0-2 2v11a2 2 0 0 0 2 2h11a2 2 0 0 0 2-2v-3" />
      <path d="M14 4h6v6" />
      <path d="M20 4l-8 8" />
    </svg>
  ),
  // Open With — app grid of four squares.
  openWith: (
    <svg {...svgProps}>
      <rect x="4" y="4" width="7" height="7" rx="1.5" />
      <rect x="13" y="4" width="7" height="7" rx="1.5" />
      <rect x="4" y="13" width="7" height="7" rx="1.5" />
      <rect x="13" y="13" width="7" height="7" rx="1.5" />
    </svg>
  ),
  // Move to Bin — trash can with lid + two ribs.
  trash: (
    <svg {...svgProps}>
      <path d="M4 7h16" />
      <path d="M9 7V5a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2" />
      <path d="M6 7l1 12a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-12" />
      <path d="M10 11v6M14 11v6" />
    </svg>
  ),
  // Rename — pencil.
  rename: (
    <svg {...svgProps}>
      <path d="M4 20h4l10.5-10.5a2.12 2.12 0 0 0-3-3L5 17v3z" />
      <path d="M13.5 6.5l3 3" />
    </svg>
  ),
  // Duplicate — two overlapping squares with a plus in the front one.
  duplicate: (
    <svg {...svgProps}>
      <path d="M8 8V6a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-2" />
      <rect x="4" y="8" width="12" height="12" rx="2" />
      <path d="M10 12v4M8 14h4" />
    </svg>
  ),
  // Cut — scissors.
  cut: (
    <svg {...svgProps}>
      <circle cx="6" cy="6" r="2.5" />
      <circle cx="6" cy="18" r="2.5" />
      <path d="M20 4L8.12 15.88" />
      <path d="M14.47 14.48L20 20" />
      <path d="M8.12 8.12L12 12" />
    </svg>
  ),
  // Copy — two stacked sheets.
  copy: (
    <svg {...svgProps}>
      <rect x="9" y="9" width="11" height="11" rx="2" />
      <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
    </svg>
  ),
  // Paste — clipboard.
  paste: (
    <svg {...svgProps}>
      <path d="M9 4H7a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V6a2 2 0 0 0-2-2h-2" />
      <rect x="9" y="3" width="6" height="4" rx="1" />
    </svg>
  ),
  // Copy Path — link chain.
  copyPath: (
    <svg {...svgProps}>
      <path d="M10 13a5 5 0 0 0 7 0l3-3a5 5 0 0 0-7-7l-1.5 1.5" />
      <path d="M14 11a5 5 0 0 0-7 0l-3 3a5 5 0 0 0 7 7l1.5-1.5" />
    </svg>
  ),
  // Reveal in Finder — the shared Finder glyph, matching the breadcrumb's
  // "Open in Finder" button (components/FinderIcon).
  reveal: <FinderIcon />,
  // Refresh — two circular arrows.
  refresh: (
    <svg {...svgProps}>
      <path d="M20 8a8 8 0 0 0-14.5-1.5L4 8" />
      <path d="M4 4v4h4" />
      <path d="M4 16a8 8 0 0 0 14.5 1.5L20 16" />
      <path d="M20 20v-4h-4" />
    </svg>
  ),
  // New File — document with a plus.
  newFile: (
    <svg {...svgProps}>
      <path d="M14 4H7a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V9z" />
      <path d="M14 4v5h5" />
      <path d="M12 12v5M9.5 14.5h5" />
    </svg>
  ),
  // New Folder — folder with a plus.
  newFolder: (
    <svg {...svgProps}>
      <path d="M4 8V6a2 2 0 0 1 2-2h3l2 2h7a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V8z" />
      <path d="M12 11v5M9.5 13.5h5" />
    </svg>
  ),
};
