// The app-icon palette: Notion's ten icon colours, each a light/dark pair, so
// a picked lucide glyph follows the shell's theme the way the generic AppStar
// (on currentColor) already does.
//
// The problem this solves: `icon.svg` is a static file drawn through `<img>`
// and `<link rel="icon">`, and neither crosses into the shell's CSS — no
// `currentColor`, no `--fg-muted`, no `data-theme`. So the picker writes the
// COLOUR'S NAME into the file (`data-fused-color="red"`) and strokes the glyph
// in `currentColor`, and the shell substitutes the hex for the live theme
// before the browser ever sees it (app-icon-src.ts). The file also carries a
// `prefers-color-scheme` fallback so it reads right standalone — Finder,
// GitHub, a bare tab — where only the OS theme is knowable.
//
// DOM-free on purpose (no fetch, no document): current-apps-lib.ts and the bun
// tests can import it.
import type { Theme } from "@platform/lib/theme";

export const ICON_COLORS = [
  "default",
  "gray",
  "brown",
  "yellow",
  "orange",
  "green",
  "blue",
  "purple",
  "pink",
  "red",
] as const;

export type IconColor = (typeof ICON_COLORS)[number];

/** Hex per theme. `default` is the `--fg-muted` pair (tokens.css) — the tint
 *  the generic AppStar fallback wears, so an uncoloured pick and no pick at
 *  all sit in the same register. The rest are Notion's icon palette. The
 *  same values live in tokens.css as `--app-icon-<name>` for the picker's
 *  swatches; the svg needs them as literals. */
export const ICON_COLOR_HEX: Record<IconColor, { light: string; dark: string }> = {
  default: { light: "#61656c", dark: "#9aa0a6" },
  gray: { light: "#787774", dark: "#9b9b9b" },
  brown: { light: "#9f6b53", dark: "#ba856f" },
  yellow: { light: "#cb912f", dark: "#ca9849" },
  orange: { light: "#d9730d", dark: "#c77d48" },
  green: { light: "#448361", dark: "#529e72" },
  blue: { light: "#337ea9", dark: "#5e87c9" },
  purple: { light: "#9065b0", dark: "#9d68d3" },
  pink: { light: "#c14c8a", dark: "#d15796" },
  red: { light: "#d44c47", dark: "#df5452" },
};

export const ICON_COLOR_LABEL: Record<IconColor, string> = {
  default: "Default",
  gray: "Gray",
  brown: "Brown",
  yellow: "Yellow",
  orange: "Orange",
  green: "Green",
  blue: "Blue",
  purple: "Purple",
  pink: "Pink",
  red: "Red",
};

export function isIconColor(v: unknown): v is IconColor {
  return typeof v === "string" && (ICON_COLORS as readonly string[]).includes(v);
}

/** The colour name an icon.svg declares on its root, or null for a file with
 *  none (an emoji glyph, a hand-authored icon) — those are drawn as they are.
 *  Only the root's attribute counts, and only a known name: the file is
 *  author-controlled, so an unknown value is "no marker", not an error. */
export function readIconColor(svg: string): IconColor | null {
  const root = svg.match(/<svg\b[^>]*>/);
  if (!root) return null;
  const m = root[0].match(/\sdata-fused-color="([a-z]+)"/);
  return m && isIconColor(m[1]) ? m[1] : null;
}

/** The svg with its `currentColor` strokes resolved to the theme's hex —
 *  what the shell actually hands the `<img>` / favicon. A file without a
 *  marker comes back untouched. */
export function themeIconSvg(svg: string, theme: Theme): string {
  const color = readIconColor(svg);
  if (!color) return svg;
  return svg.replace(/currentColor/g, ICON_COLOR_HEX[color][theme]);
}

/** A data: URL for an svg document. encodeURIComponent, not base64: the
 *  result is readable in devtools and about a third smaller for this text. */
export function svgDataUrl(svg: string): string {
  return "data:image/svg+xml;charset=utf-8," + encodeURIComponent(svg);
}
