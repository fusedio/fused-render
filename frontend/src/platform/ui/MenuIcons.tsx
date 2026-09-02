// Inline monochrome icons for menus drawn with @platform/ui/ContextMenu — the
// file explorer's right-click menus first, and since D426 the AI models page's
// task/sort dropdowns too. ONE table, because a glyph is a word: a funnel that
// means "narrow this down" in one menu and something else in another is a
// vocabulary with two dialects. Same house
// style as FileIcons/SplitIcons but tuned to match macOS Finder's menu icons:
// 16x16, viewBox 0 0 24 24, fill none, stroke currentColor at a lighter 1.5px
// weight, round caps/joins. Hand-written Lucide-geometry paths — no npm
// dependency. The glyphs are colourless and inherit the row's colour via
// currentColor (so a danger row tints its icon red for free).
import type { ReactNode } from "react";
import { FinderIcon } from "@platform/ui/FinderIcon";

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
  // Camera — a capture of what is on screen ("Set Current View as Preview").
  camera: (
    <svg {...svgProps}>
      <path d="M4 8.5A1.5 1.5 0 0 1 5.5 7h2.3l1.4-2h5.6l1.4 2h2.3A1.5 1.5 0 0 1 20 8.5v9A1.5 1.5 0 0 1 18.5 19h-13A1.5 1.5 0 0 1 4 17.5z" />
      <circle cx="12" cy="13" r="3.2" />
    </svg>
  ),
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
  // Open in Explorer — plain folder (newFolder's body without the plus): the
  // internal explorer's listing of a directory, as opposed to `reveal`, which
  // is the OS file manager.
  folder: (
    <svg {...svgProps}>
      <path d="M4 8V6a2 2 0 0 1 2-2h3l2 2h7a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V8z" />
    </svg>
  ),
  // Open in New Tab — browser window (tab strip across the top) with a plus in
  // the body. Distinct from `open` (an arrow out of a box: this same page
  // navigating) and from `folder`: the target lands in ANOTHER tab.
  newTab: (
    <svg {...svgProps}>
      <rect x="3" y="4" width="18" height="16" rx="2" />
      <path d="M3 9h18" />
      <path d="M12 12v5M9.5 14.5h5" />
    </svg>
  ),
  // Delete — trash can with lid + two ribs.
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
  // Compress — archive box: a lid band over a body, with a latch.
  compress: (
    <svg {...svgProps}>
      <rect x="3" y="4" width="18" height="4.5" rx="1" />
      <path d="M5 8.5V18a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8.5" />
      <path d="M10 12h4" />
    </svg>
  ),
  // Download — arrow dropping into a tray. Distinct from `compress` (an
  // archive box): exporting an app file is a download to the user, while
  // Compress writes an archive beside the folder and never leaves the disk.
  download: (
    <svg {...svgProps}>
      <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
      <path d="M7 10l5 5 5-5" />
      <path d="M12 15V3" />
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

  // ---- Choosing rather than doing -----------------------------------------
  // The five below are for menus that are a set of ALTERNATIVES (the AI models
  // page's task filter and result sort, D426) rather than a list of actions.
  // They live here with the rest because the vocabulary is shared: a funnel has
  // to mean "narrow this down" in whatever menu it appears in, and a second
  // hand-rolled funnel three directories away is how one meaning becomes two
  // glyphs. Downloads deliberately has no entry of its own — it reuses
  // `download` above, an arrow into a tray, which is exactly what a download
  // COUNT is a count of.

  // Filter — funnel. "Show me only some of these", which is what a task filter
  // does; distinct from `openWith`'s grid of choices, which switches WHICH tool
  // rather than narrowing a set.
  filter: (
    <svg {...svgProps}>
      <path d="M20 4H4l6.5 8v6l3 1.5V12L20 4z" />
    </svg>
  ),
  // Likes — heart. The Hub's own word for the count, and its own glyph for it.
  heart: (
    <svg {...svgProps}>
      <path d="M12 20.5l-6.4-6.4A4.5 4.5 0 0 1 12 7.8a4.5 4.5 0 0 1 6.4 6.3L12 20.5z" />
    </svg>
  ),
  // Updated — clock. NOT `refresh` (two circular arrows), which is already the
  // page's glyph for "fetch this again" — an action the reader can take. This
  // is a fact about the repo: when it last changed.
  clock: (
    <svg {...svgProps}>
      <circle cx="12" cy="12" r="8.5" />
      <path d="M12 7.5V12l3.2 2" />
    </svg>
  ),
  // New — sparkle. Recently PUBLISHED, which is not the same fact as recently
  // changed, so it cannot share the clock.
  sparkle: (
    <svg {...svgProps}>
      <path d="M11 3.5l1.6 4.4 4.4 1.6-4.4 1.6L11 15.5 9.4 11.1 5 9.5l4.4-1.6L11 3.5z" />
      <path d="M17.5 15l.8 2 2 .8-2 .8-.8 2-.8-2-2-.8 2-.8.8-2z" />
    </svg>
  ),
  // Size — hard drive. The figure a size sort ranks by is bytes you will have to
  // store, so the glyph is the thing they land on rather than a scale or a
  // ruler: the reader's question is "what fits".
  drive: (
    <svg {...svgProps}>
      <path d="M5.6 5.1L3 11v6a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-6l-2.6-5.9A2 2 0 0 0 16.6 4H7.4a2 2 0 0 0-1.8 1.1z" />
      <path d="M3 11h18" />
      <path d="M6.5 15h.01M10 15h.01" />
    </svg>
  ),

  // ---- Benchmark tab buttons (SPEC AI-14) ----------------------------------
  // These four replace text labels with icons on the Benchmark tab; each
  // caller keeps the word it replaced as both `aria-label` and `title`, so the
  // glyph is a shorthand for the label rather than its only carrier.

  // Run — a plain outline play triangle. Doubles for both "Run benchmark" and
  // "Run again": the action is identical, only the tooltip's wording differs.
  play: (
    <svg {...svgProps}>
      <path d="M8 5l11 7-11 7z" />
    </svg>
  ),
  // Running — a partial ring, spun by `.am-icon-spin` (ai-models.css) rather
  // than baked into the path, so the SAME glyph works in a static context
  // (none today, but a second consumer should not need a second path). Not
  // `refresh` (a full two-arrow loop, already meaning "fetch this again") —
  // this is a plainer arc, the shape most spinners actually use.
  spinner: (
    <svg {...svgProps}>
      <path d="M20 12a8 8 0 1 1-2.34-5.66" />
    </svg>
  ),
  // Details disclosure — a chevron. Rotated 90° by `[open] > summary` in
  // ai-models.css rather than swapped for a second path: a `<details>` is
  // already the open/closed state, and a CSS rotation of one glyph is a
  // smaller vocabulary than a "closed" and an "open" glyph that must always
  // agree with each other.
  chevron: (
    <svg {...svgProps}>
      <path d="M9 6l6 6-6 6" />
    </svg>
  ),
  // Share — the three-node graph: one point on the left, two on the right,
  // joined by the two edges between them. The universal "send this somewhere
  // else" glyph, and it reads at 16px in a way the tray-plus-arrow it replaced
  // did not: that one was a stack of three near-parallel strokes that blurred
  // into `download`'s mirror image at button size, while three round nodes stay
  // distinct shapes however small the button gets.
  share: (
    <svg {...svgProps}>
      <circle cx="18" cy="5" r="2.6" />
      <circle cx="6" cy="12" r="2.6" />
      <circle cx="18" cy="19" r="2.6" />
      <path d="M8.4 10.8l7.2-4.2" />
      <path d="M8.4 13.2l7.2 4.2" />
    </svg>
  ),
  // Stop — a plain filled-outline square, the universal "halt" glyph and
  // distinct from `trash` (this does not delete anything that ran).
  stop: (
    <svg {...svgProps}>
      <rect x="6" y="6" width="12" height="12" rx="1.5" />
    </svg>
  ),
  // Info — a circle with a dot above a stem, the universal "there is more to
  // read here" glyph. Its one consumer (the Benchmark tab's per-capability
  // workload explanation, D483) wraps it in a real `<button>` carrying
  // `data-hint` (platform/lib/hints.ts, D474) rather than a native `title` —
  // this glyph is the announced control, never the caption itself, so it
  // stays the plain outline every other glyph here is.
  info: (
    <svg {...svgProps}>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 11v5" />
      <path d="M12 8h.01" />
    </svg>
  ),
  // A target/bullseye — "Best match" (D639) is the one ordering that is a
  // judgement blending several facts into a single ranking rather than a
  // fact about the repo itself, and no existing glyph here reads as
  // "ranked for you" without already meaning something else in this menu.
  target: (
    <svg {...svgProps}>
      <circle cx="12" cy="12" r="8" />
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2v3M12 19v3M2 12h3M19 12h3" />
    </svg>
  ),
};
