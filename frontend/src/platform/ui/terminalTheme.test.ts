// Coverage for terminalTheme.ts's pure theme-building function — see
// TerminalView.tsx's own header for why the component that actually calls it
// stays untested and this module carries the logic instead.
import { describe, expect, it } from "bun:test";

import { buildTerminalTheme, terminalFontFamily, type CssVarLookup } from "./terminalTheme";

function lookupFrom(vars: Record<string, string>): CssVarLookup {
  return (name: string) => vars[name] ?? "";
}

describe("buildTerminalTheme", () => {
  it("reads foreground/cursor/selection from the app's dark tokens", () => {
    const theme = buildTerminalTheme(
      "dark",
      lookupFrom({ "--fg": "#e8eaed", "--bg": "#131417", "--sel": "#2b3a52" }),
    );
    expect(theme.background).toBe("transparent");
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

  it("keeps background transparent regardless of theme", () => {
    expect(buildTerminalTheme("dark", lookupFrom({})).background).toBe("transparent");
    expect(buildTerminalTheme("light", lookupFrom({})).background).toBe("transparent");
  });
});

describe("terminalFontFamily", () => {
  it("uses the app's --font-mono token when present", () => {
    expect(terminalFontFamily(lookupFrom({ "--font-mono": "MyMono, monospace" }))).toBe(
      "MyMono, monospace",
    );
  });

  it("falls back to the system monospace stack when the token is unset", () => {
    expect(terminalFontFamily(lookupFrom({}))).toBe(
      "ui-monospace, SFMono-Regular, Menlo, monospace",
    );
  });
});
