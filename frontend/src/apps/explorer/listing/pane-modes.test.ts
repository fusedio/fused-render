import { describe, expect, test } from "bun:test";
import type { TemplateEntry } from "@platform/lib/api";
import {
  PANE_APP_MODE,
  PANE_NONE_MODE,
  activePaneMode,
  paneHasRealMode,
  paneModeList,
} from "./pane-modes";

// The universal `/` directory key as the built-in registry ships it (SPEC
// PT-13): `_listing` first and unconditional, every peer condition.py-gated.
function dirTemplates(modes: string[] = ["_listing", "app", "claude", "versions", "git", "graph"]): TemplateEntry[] {
  return modes.map((mode) => ({
    mode,
    path: mode === "_listing" ? null : `/t/${mode}/template.html`,
    icon: null,
    ...(mode === "_listing" ? {} : { conditional: true }),
  })) as TemplateEntry[];
}

const allow = (...modes: string[]) => Object.fromEntries(modes.map((m) => [m, true]));

describe("paneModeList — self target (nothing selected)", () => {
  test("defaults to the neutral placeholder, never the chat", () => {
    const modes = paneModeList({
      templates: dirTemplates(),
      conditions: allow("claude", "git"),
      isDir: true,
      self: true,
      hasApp: false,
    });
    expect(modes[0]).toBe(PANE_NONE_MODE);
    expect(activePaneMode(modes, null)).toBe(PANE_NONE_MODE);
    // The opt-in modes are still offered — one click away in the switcher.
    expect(modes).toContain("claude");
    expect(modes).toContain("git");
    // Its own listing is never re-shown; that listing is the left half.
    expect(modes).not.toContain("_listing");
    // More than one entry, so ModeSwitcher renders (PT-10) and the chat is
    // reachable from the header.
    expect(modes.length).toBeGreaterThan(1);
    expect(paneHasRealMode(modes)).toBe(true);
  });

  test("a lone-app folder still leads with its own app, `_none` behind it", () => {
    const modes = paneModeList({
      templates: dirTemplates(),
      conditions: allow("claude"),
      isDir: true,
      self: true,
      hasApp: true,
    });
    // The folder's app is a preview of the folder, not an opt-in tool — it
    // keeps the default; `_none` stays offered as the way to clear the pane.
    expect(modes).toEqual([PANE_APP_MODE, PANE_NONE_MODE, "claude"]);
    expect(activePaneMode(modes, null)).toBe(PANE_APP_MODE);
  });

  test("nothing offerable leaves only the placeholder (bare hint, no header)", () => {
    const modes = paneModeList({
      templates: dirTemplates(),
      conditions: {},
      isDir: true,
      self: true,
      hasApp: false,
    });
    expect(modes).toEqual([PANE_NONE_MODE]);
    expect(paneHasRealMode(modes)).toBe(false);
  });

  test("an explicit choice still wins over the neutral default", () => {
    const modes = paneModeList({
      templates: dirTemplates(),
      conditions: allow("claude"),
      isDir: true,
      self: true,
      hasApp: false,
    });
    // From the switcher, or seeded from the `_panelMode` URL param.
    expect(activePaneMode(modes, "claude")).toBe("claude");
    // A mode this target does not offer falls back to the default (PT-9).
    expect(activePaneMode(modes, "versions")).toBe(PANE_NONE_MODE);
  });
});

describe("paneModeList — selected (non-self) target", () => {
  test("a selected subfolder still defaults to the embedded listing", () => {
    const modes = paneModeList({
      templates: dirTemplates(),
      conditions: allow("claude", "git"),
      isDir: true,
      self: false,
      hasApp: false,
    });
    expect(modes[0]).toBe("_listing");
    expect(activePaneMode(modes, null)).toBe("_listing");
    expect(modes).not.toContain(PANE_NONE_MODE);
  });

  test("a lone-app folder leads with `_app`, listing next", () => {
    const modes = paneModeList({
      templates: dirTemplates(),
      conditions: allow("claude"),
      isDir: true,
      self: false,
      hasApp: true,
    });
    expect(modes).toEqual([PANE_APP_MODE, "_listing", "claude"]);
  });

  test("a file drops `_listing` and takes its own first mode", () => {
    const templates = [
      { mode: "_render", path: null, icon: null },
      { mode: "code", path: "/t/code/template.html", icon: null },
      { mode: "_listing", path: null, icon: null },
    ] as TemplateEntry[];
    const modes = paneModeList({ templates, conditions: {}, isDir: false, self: false, hasApp: false });
    expect(modes).toEqual(["_render", "code"]);
  });

  test("a gated mode is not offered until its verdict allows (CT-12)", () => {
    const templates = dirTemplates(["_listing", "zarr_aoi"]);
    const pending = paneModeList({ templates, conditions: null, isDir: true, self: false, hasApp: false });
    expect(pending).toEqual(["_listing"]);
    const allowed = paneModeList({
      templates,
      conditions: allow("zarr_aoi"),
      isDir: true,
      self: false,
      hasApp: false,
    });
    expect(allowed).toEqual(["_listing", "zarr_aoi"]);
  });
});
