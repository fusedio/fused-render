// Item B: the hit row's stats cell used to spell out downloads/likes/age with
// text glyphs ("↓", "♥") that read inconsistently against the mono digits
// beside them — a down-arrow and a heart drawn in two different type systems
// (Unicode glyph vs. the row's own monospace numerals). These are a small,
// matched inline-SVG set instead: lucide-style outline icons (24x24 source
// viewBox, `stroke-width` 1.75, `currentColor` so they inherit the cell's own
// muted colour and match dark/light without a second token), rendered at a
// fixed 12px. Each keeps the `aria-label` the old glyph's `title` carried, so
// a screen reader still gets "downloads"/"likes"/"updated" — an `aria-hidden`
// SVG plus a visually-hidden label would say the same thing with more markup
// for no more information.
import type { SVGProps } from "react";

/** Shared geometry every stat icon in this row wants: 12px square, the row's
 *  own `currentColor`, and a −1px baseline nudge so the icon's optical centre
 *  lines up with the mono digits' baseline rather than their box's centre. */
function StatIcon({ children, ...rest }: SVGProps<SVGSVGElement>) {
  return (
    <svg
      viewBox="0 0 24 24"
      width="12"
      height="12"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.75}
      strokeLinecap="round"
      strokeLinejoin="round"
      style={{ verticalAlign: "middle", marginBottom: "-1px" }}
      {...rest}
    >
      {children}
    </svg>
  );
}

/** A tray with a down arrow — downloads. Callers pass `aria-label` (this row
 *  always does — "downloads"/"likes"/"updated") so the icon reads to a
 *  screen reader instead of disappearing as decoration. */
export function DownloadIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <StatIcon {...props}>
      <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
      <polyline points="7 10 12 15 17 10" />
      <line x1="12" y1="15" x2="12" y2="3" />
    </StatIcon>
  );
}

/** A heart — likes. */
export function HeartIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <StatIcon {...props}>
      <path d="M19 14c1.49-1.46 3-3.21 3-5.5A5.5 5.5 0 0 0 16.5 3c-1.76 0-3 .5-4.5 2-1.5-1.5-2.74-2-4.5-2A5.5 5.5 0 0 0 2 8.5c0 2.3 1.5 4.05 3 5.5l7 7Z" />
    </StatIcon>
  );
}

/** A clock face — last-updated age. */
export function ClockIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <StatIcon {...props}>
      <circle cx="12" cy="12" r="10" />
      <polyline points="12 6 12 12 16 14" />
    </StatIcon>
  );
}
