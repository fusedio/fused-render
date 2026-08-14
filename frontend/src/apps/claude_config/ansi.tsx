// Minimal ANSI SGR renderer for the statusline preview: reset / bold / dim and
// the basic foreground colours, so the preview reads the way the terminal does.
// Unknown codes are ignored (a statusline is not a terminal emulator).
//
// The original app mapped each code to a hardcoded hex tuned for its own dark
// canvas. Here a code maps to a CLASS instead, and styles/claude-config.css
// resolves that class to a shell palette token — so the preview follows
// light/dark like everything else on the page, and no colour literal has to
// live in a stylesheet (tests/test_theme.py).
//
// Consequence worth stating: the bright variants (90–97) resolve to the same
// eight hues as their normal counterparts. The shell palette has one token per
// hue, and inventing a second tier of them for a status-line preview would add
// sixteen tokens to the app's vocabulary to encode a distinction almost no
// statusline draws. `bold` still separates emphasis from plain.
import type { ReactNode } from "react";

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
    s.hue ? "cc-ansi-" + s.hue : "",
    s.bold ? "cc-ansi-bold" : "",
    s.dim ? "cc-ansi-dim" : "",
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
