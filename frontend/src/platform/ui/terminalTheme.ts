// Builds the xterm.js `ITheme` the status-bar terminal paints with, from the
// app's own CSS custom properties (styles/tokens.css) rather than xterm's
// built-in defaults — those default to white-on-Tango, which is unreadable
// once the terminal sits on the light palette's white `.term-drawer`
// background.
//
// A pure function, not a hook: `TerminalView.tsx` (deliberately untested —
// see its own header) supplies the live `getComputedStyle` lookup and calls
// this once at mount and again on every theme change; this module is the one
// piece of that logic worth a unit test on its own, given a fake lookup.
import type { ITheme } from "@xterm/xterm";
import type { Theme } from "@platform/lib/theme";

/** A CSS custom property's resolved value, trimmed — what
 * `getComputedStyle(document.documentElement).getPropertyValue(name)` gives,
 * minus the leading space every browser puts after the colon. */
export type CssVarLookup = (name: string) => string;

/** The real lookup: reads the live cascade off `<html>`, where
 * `styles/tokens.css` defines `--fg`/`--bg`/`--sel`/etc. per `[data-theme]`
 * (platform/lib/theme.ts). */
export function documentCssVarLookup(): CssVarLookup {
  const style = getComputedStyle(document.documentElement);
  return (name: string) => style.getPropertyValue(name).trim();
}

/** Fallbacks for a lookup that comes back empty (a test's fake
 * `getComputedStyle`, or a stylesheet that hasn't loaded yet) — the same
 * hex values `styles/tokens.css` itself carries for `--fg`/`--bg`/`--sel`,
 * so a missing token still resolves to the real palette rather than to
 * xterm's own black-on-white/white-on-black guess. */
const FALLBACK = {
  dark: { fg: "#e8eaed", bg: "#131417", sel: "#2b3a52" },
  light: { fg: "#1f2023", bg: "#ffffff", sel: "#cfe0f7" },
} as const;

/** Two curated 16-color ANSI palettes (GitHub's own light/dark terminal
 * theme), tuned so bright yellow and bright white — the two hues xterm's
 * stock Tango palette gets most wrong against a pale background — stay
 * legible on both `--bg` values rather than only on a dark one. */
const ANSI_DARK: Pick<
  ITheme,
  | "black" | "red" | "green" | "yellow" | "blue" | "magenta" | "cyan" | "white"
  | "brightBlack" | "brightRed" | "brightGreen" | "brightYellow" | "brightBlue"
  | "brightMagenta" | "brightCyan" | "brightWhite"
> = {
  black: "#484f58",
  red: "#ff7b72",
  green: "#3fb950",
  yellow: "#d29922",
  blue: "#58a6ff",
  magenta: "#bc8cff",
  cyan: "#39c5cf",
  white: "#b1bac4",
  brightBlack: "#6e7681",
  brightRed: "#ffa198",
  brightGreen: "#56d364",
  brightYellow: "#e3b341",
  brightBlue: "#79c0ff",
  brightMagenta: "#d2a8ff",
  brightCyan: "#56d4dd",
  brightWhite: "#f0f6fc",
};

const ANSI_LIGHT: typeof ANSI_DARK = {
  black: "#24292f",
  red: "#cf222e",
  green: "#116329",
  yellow: "#4d2d00",
  blue: "#0969da",
  magenta: "#8250df",
  cyan: "#1b7c83",
  white: "#6e7781",
  brightBlack: "#57606a",
  brightRed: "#a40e26",
  brightGreen: "#1a7f37",
  brightYellow: "#633c01",
  brightBlue: "#218bff",
  brightMagenta: "#a475f9",
  brightCyan: "#3192aa",
  brightWhite: "#8c959f",
};

/** The xterm theme for the given app theme. Background stays transparent —
 * `.term-drawer` (styles/notifications.css) already paints `--bg` behind the
 * whole drawer, so xterm's own canvas has nothing to fill; every other slot
 * comes from the app's tokens (`lookup`) with the matching palette's own
 * hex as a fallback when a token resolves empty. */
export function buildTerminalTheme(theme: Theme, lookup: CssVarLookup): ITheme {
  const fallback = FALLBACK[theme];
  const fg = lookup("--fg") || fallback.fg;
  const bg = lookup("--bg") || fallback.bg;
  const selectionBackground = lookup("--sel") || fallback.sel;
  const ansi = theme === "light" ? ANSI_LIGHT : ANSI_DARK;

  return {
    background: "transparent",
    foreground: fg,
    // A block cursor's own fill is `cursor`, and the glyph drawn inside it
    // is `cursorAccent` — swapping fg/bg for those two is what keeps the
    // cursor readable as a solid block rather than fg-on-fg.
    cursor: fg,
    cursorAccent: bg,
    selectionBackground,
    ...ansi,
  };
}

/** The app's monospace stack (styles/base.css's `--font-mono` token, with the
 * same system fallback that CSS rule itself falls back to) — xterm defaults
 * to Courier New otherwise, which does not match any other code surface in
 * the app. */
export function terminalFontFamily(lookup: CssVarLookup): string {
  return (
    lookup("--font-mono") ||
    "ui-monospace, SFMono-Regular, Menlo, monospace"
  );
}
