// Coverage for terminalTheme.ts's pure theme-building function — see
// TerminalView.tsx's own header for why the component that actually calls it
// stays untested and this module carries the logic instead.
import { afterEach, describe, expect, it } from "bun:test";

import {
  buildTerminalTheme,
  loadTerminalFont,
  terminalFontFamily,
  TERMINAL_NERD_FONT,
  type CssVarLookup,
} from "./terminalTheme";

function lookupFrom(vars: Record<string, string>): CssVarLookup {
  return (name: string) => vars[name] ?? "";
}

describe("buildTerminalTheme", () => {
  it("reads foreground/cursor/selection from the app's dark tokens", () => {
    const theme = buildTerminalTheme(
      "dark",
      lookupFrom({ "--fg": "#e8eaed", "--bg": "#131417", "--sel": "#2b3a52" }),
    );
    expect(theme.background).toBe("#131417");
    expect(theme.foreground).toBe("#e8eaed");
    expect(theme.cursor).toBe("#e8eaed");
    expect(theme.cursorAccent).toBe("#131417");
    expect(theme.selectionBackground).toBe("#2b3a52");
  });

  it("reads foreground/cursor/selection from the app's light tokens", () => {
    const theme = buildTerminalTheme(
      "light",
      lookupFrom({ "--fg": "#1f2023", "--bg": "#ffffff", "--sel": "#cfe0f7" }),
    );
    expect(theme.foreground).toBe("#1f2023");
    expect(theme.cursor).toBe("#1f2023");
    expect(theme.cursorAccent).toBe("#ffffff");
    expect(theme.selectionBackground).toBe("#cfe0f7");
  });

  it("falls back to the matching palette's own colours when a token resolves empty", () => {
    const theme = buildTerminalTheme("dark", lookupFrom({}));
    expect(theme.foreground).toBe("#e8eaed");
    expect(theme.cursorAccent).toBe("#131417");
    expect(theme.selectionBackground).toBe("#2b3a52");
  });

  it("picks the dark ANSI palette in dark mode and the light one in light mode", () => {
    const dark = buildTerminalTheme("dark", lookupFrom({}));
    const light = buildTerminalTheme("light", lookupFrom({}));
    expect(dark.yellow).not.toBe(light.yellow);
    expect(dark.brightYellow).not.toBe(light.brightYellow);
    expect(dark.brightWhite).not.toBe(light.brightWhite);
    // Both bright-yellow and bright-white stay distinct from the light
    // palette's own background so neither goes invisible on a white pane.
    expect(light.brightYellow?.toLowerCase()).not.toBe("#ffffff");
    expect(light.brightWhite?.toLowerCase()).not.toBe("#ffffff");
  });

  it("paints the opaque --bg (never transparent: xterm renders black without allowTransparency)", () => {
    expect(buildTerminalTheme("dark", lookupFrom({})).background).toBe("#131417");
    expect(buildTerminalTheme("light", lookupFrom({})).background).toBe("#ffffff");
    expect(buildTerminalTheme("light", lookupFrom({ "--bg": "#fafafa" })).background).toBe("#fafafa");
  });
});

describe("terminalFontFamily", () => {
  it("puts the bundled Nerd Font first, then the app's --font-mono token", () => {
    expect(terminalFontFamily(lookupFrom({ "--font-mono": "MyMono, monospace" }))).toBe(
      `"${TERMINAL_NERD_FONT}", MyMono, monospace`,
    );
  });

  it("falls back to the system monospace stack when the token is unset", () => {
    expect(terminalFontFamily(lookupFrom({}))).toBe(
      `"${TERMINAL_NERD_FONT}", ui-monospace, SFMono-Regular, Menlo, monospace`,
    );
  });
});

describe("loadTerminalFont", () => {
  const g = globalThis as { document?: unknown };
  const saved = g.document;
  afterEach(() => {
    g.document = saved;
  });

  it("resolves true once both weights have loaded", async () => {
    const asked: string[] = [];
    g.document = { fonts: { load: async (q: string) => { asked.push(q); return []; } } };
    expect(await loadTerminalFont(500)).toBe(true);
    expect(asked.some((q) => q.includes(TERMINAL_NERD_FONT) && q.startsWith("bold"))).toBe(true);
    expect(asked.some((q) => q.includes(TERMINAL_NERD_FONT) && q.startsWith("12px"))).toBe(true);
  });

  it("gives up after the timeout instead of hanging the mount", async () => {
    g.document = { fonts: { load: () => new Promise(() => {}) } };
    expect(await loadTerminalFont(20)).toBe(false);
  });

  it("resolves false (never rejects) when the load throws or fonts are unsupported", async () => {
    g.document = { fonts: { load: async () => { throw new Error("nope"); } } };
    expect(await loadTerminalFont(500)).toBe(false);
    g.document = {};
    expect(await loadTerminalFont(500)).toBe(false);
  });
});
