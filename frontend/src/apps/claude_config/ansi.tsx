// Minimal ANSI SGR renderer for the statusline preview: reset / bold / dim and
// the basic foreground colours, so the preview reads the way the terminal does.
// Unknown codes are ignored (a statusline is not a terminal emulator).
//
// A code maps to Tailwind classes, not a hex: the preview sits in the dark
// log-viewer block (bg-neutral-950), so the hues are the status-colour buckets
// from status-colors.ts where a bucket exists (red / green / yellow / blue) and
// neutral greys otherwise. Cyan borrows the blue bucket (its nearest hue);
// magenta has no bucket and no neutral home, so it renders as the base text —
// bold/dim still separate it from plain. The bright variants (90–97) resolve
// to the same hues as their normal counterparts; the block is always dark, so
// there is no light-theme tier to pick from.
import type { ReactNode } from "react";
import { bucketText } from "@platform/ui/status-colors";

type Hue = "gray" | "red" | "green" | "yellow" | "blue" | "magenta" | "cyan" | "white";

const FG: Record<number, Hue> = {
  30: "gray",
  31: "red",
  32: "green",
  33: "yellow",
  34: "blue",
  35: "magenta",
  36: "cyan",
  37: "white",
  90: "gray",
  91: "red",
  92: "green",
  93: "yellow",
  94: "blue",
  95: "magenta",
  96: "cyan",
  97: "white",
};

// The dark block never flips with the theme, so the `dark:` half of each bucket
// pair is what should show: pick it out rather than depend on the ancestor.
const darkOf = (pair: string) => pair.split(" ").find((c) => c.startsWith("dark:"))?.slice(5) ?? pair;

// The log block's error tint — the same red-400 the ANSI red resolves to.
export const LOG_ERROR_CLASS = darkOf(bucketText.red);

const HUE_CLASS: Record<Hue, string | undefined> = {
  gray: "text-neutral-400",
  red: darkOf(bucketText.red),
  green: darkOf(bucketText.green),
  yellow: darkOf(bucketText.yellow),
  blue: darkOf(bucketText.blue),
  magenta: undefined,
  cyan: darkOf(bucketText.blue),
  white: "text-neutral-50",
};

interface Style {
  hue?: Hue;
  bold?: boolean;
  dim?: boolean;
}

// Built with RegExp rather than as a literal: an SGR sequence begins with the
// ESC control character, and a raw control byte inside a regex literal is both
// invisible in a diff and the exact shape lint rules flag. Named once instead.
const ESC = "\u001b";
const SPLIT = new RegExp("(" + ESC + "\\[[0-9;]*m)");
const SGR = new RegExp("^" + ESC + "\\[([0-9;]*)m$");

function classOf(s: Style): string | undefined {
  const parts = [
    s.hue ? HUE_CLASS[s.hue] : "",
    s.bold ? "font-semibold" : "",
    s.dim ? "opacity-60" : "",
  ].filter(Boolean);
  return parts.length ? parts.join(" ") : undefined;
}

// Split `input` on SGR sequences, tracking the style each run of text inherits.
export function renderAnsi(input: string): ReactNode[] {
  let style: Style = {};
  const out: ReactNode[] = [];
  for (const part of input.split(SPLIT)) {
    if (part === "") continue;
    const m = part.match(SGR);
    if (m) {
      const codes = m[1] === "" ? [0] : m[1].split(";").map(Number);
      for (const code of codes) {
        if (code === 0) style = {};
        else if (code === 1) style = { ...style, bold: true };
        else if (code === 2) style = { ...style, dim: true };
        else if (code === 22) style = { ...style, bold: false, dim: false };
        else if (code === 39) style = { ...style, hue: undefined };
        else if (FG[code]) style = { ...style, hue: FG[code] };
      }
      continue;
    }
    out.push(
      <span key={out.length} className={classOf(style)}>
        {part}
      </span>,
    );
  }
  return out;
}
