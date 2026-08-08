import { describe, expect, test } from "bun:test";
import type { TemplateEntry } from "@platform/lib/api";
import { KNOWN_SENTINEL_MODES } from "@apps/explorer/ModeSwitcher";
import { PANE_APP_MODE, activePaneMode, paneModeList } from "./pane-modes";

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

// A COMPLETE verdict map — the shape `/api/fs/conditions` actually returns: one
// key per gated mode. Tests spell out every mode they care about, because a
// MISSING key no longer means "denied" (lib/mode-visibility: an absent verdict
// is a failed transport, and the entry stays visible). `{}` is therefore not
// "nothing allowed" but "the call failed", and has its own test below.
const verdicts = (allowed: string[], denied: string[] = []): Record<string, boolean> => ({
  ...Object.fromEntries(allowed.map((m) => [m, true])),
  ...Object.fromEntries(denied.map((m) => [m, false])),
});

const DIR_PEERS = ["app", "claude", "versions", "git", "graph"];

describe("paneModeList — self target (nothing selected)", () => {
  test("offers only real modes and lands on NONE of them", () => {
    const modes = paneModeList({
      templates: dirTemplates(),
      conditions: verdicts(["claude", "git"], ["app", "versions", "graph"]),
      isDir: true,
      self: true,
      hasApp: false,
    });
    // "Nothing previewed" is a STATE, not an entry: the list carries real
    // modes only, and the self target simply has no default among them.
    expect(activePaneMode(modes, null, { self: true, hasApp: false })).toBeNull();
    // The bug this pins: with `_listing` dropped, `modes[0]` is a heavyweight
    // opt-in (the chat), and it must NOT become the self target's default —
    // merely opening the pane must not open a chat on the user's folder.
    expect(modes[0]).toBe("claude");
    expect(activePaneMode(modes, null, { self: true, hasApp: false })).not.toBe("claude");
    // The opt-in modes are still offered — one click away in the switcher.
    expect(modes).toContain("claude");
    expect(modes).toContain("git");
    // Its own listing is never re-shown; that listing is the left half.
    expect(modes).not.toContain("_listing");
  });

  test("a lone-app folder still leads with its own app, and defaults to it", () => {
    const modes = paneModeList({
      templates: dirTemplates(["_listing", "claude"]),
      conditions: verdicts(["claude"]),
      isDir: true,
      self: true,
      hasApp: true,
    });
    // The folder's app is a preview of the folder, not an opt-in tool — it
    // keeps the default even for a self target.
    expect(modes).toEqual([PANE_APP_MODE, "claude"]);
    expect(activePaneMode(modes, null, { self: true, hasApp: true })).toBe(PANE_APP_MODE);
  });

  test("every gate denied leaves an empty list (bare hint, no header)", () => {
    const modes = paneModeList({
      templates: dirTemplates(),
      conditions: verdicts([], DIR_PEERS),
      isDir: true,
      self: true,
      hasApp: false,
    });
    expect(modes).toEqual([]);
    expect(activePaneMode(modes, null, { self: true, hasApp: false })).toBeNull();
  });

  test("an explicit choice still wins over having no default", () => {
    const modes = paneModeList({
      templates: dirTemplates(),
      conditions: verdicts(["claude"], ["app", "versions", "git", "graph"]),
      isDir: true,
      self: true,
      hasApp: false,
    });
    // From the switcher, or seeded from the `_panelMode` URL param.
    expect(activePaneMode(modes, "claude", { self: true, hasApp: false })).toBe("claude");
    // A mode this target does not offer falls back to the default (PT-9),
    // which for a self target is "nothing previewed".
    expect(activePaneMode(modes, "versions", { self: true, hasApp: false })).toBeNull();
  });
});

describe("paneModeList — selected (non-self) target", () => {
  test("a selected subfolder still defaults to the embedded listing", () => {
    const modes = paneModeList({
      templates: dirTemplates(),
      conditions: verdicts(["claude", "git"], ["app", "versions", "graph"]),
      isDir: true,
      self: false,
      hasApp: false,
    });
    expect(modes[0]).toBe("_listing");
    expect(activePaneMode(modes, null)).toBe("_listing");
  });

  test("a lone-app folder leads with `_app`, listing next", () => {
    const modes = paneModeList({
      templates: dirTemplates(["_listing", "claude"]),
      conditions: verdicts(["claude"]),
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
});

// A lone app has two possible carriers — the registry's `app` template and the
// pane's own `_app` sentinel — and mode-name.ts names them ALIKE on purpose
// (same view, two surfaces). The list is where the duplicate is prevented, so
// this is the rule, not a naming exemption.
describe("paneModeList — a lone app gets exactly one entry", () => {
  test("a visible registry `app` suppresses the sentinel and takes the lead", () => {
    const modes = paneModeList({
      templates: dirTemplates(["_listing", "app", "claude"]),
      conditions: verdicts(["app", "claude"]),
      isDir: true,
      self: false,
      hasApp: true,
    });
    expect(modes).toEqual(["app", "_listing", "claude"]);
    expect(modes).not.toContain(PANE_APP_MODE);
  });

  test("a self target's hoisted registry `app` is still its default", () => {
    const modes = paneModeList({
      templates: dirTemplates(["_listing", "app", "claude"]),
      conditions: verdicts(["app", "claude"]),
      isDir: true,
      self: true,
      hasApp: true,
    });
    // Same reasoning as the `_app` sentinel: the folder's own app is what the
    // folder IS, so it defaults even though nothing is selected.
    expect(modes).toEqual(["app", "claude"]);
    expect(activePaneMode(modes, null, { self: true, hasApp: true })).toBe("app");
  });

  test("an `app` binding with no lone page is not a self default", () => {
    const modes = paneModeList({
      templates: dirTemplates(["_listing", "app", "claude"]),
      conditions: verdicts(["app", "claude"]),
      isDir: true,
      self: true,
      hasApp: false,
    });
    // `app` only leads here because `_listing` was dropped, not because this
    // folder has an app to show — so the self target still has no default.
    expect(modes).toEqual(["app", "claude"]);
    expect(activePaneMode(modes, null, { self: true, hasApp: false })).toBeNull();
  });

  test("a denied registry `app` hands the lead back to the sentinel", () => {
    const modes = paneModeList({
      templates: dirTemplates(["_listing", "app", "claude"]),
      conditions: verdicts(["claude"], ["app"]),
      isDir: true,
      self: false,
      hasApp: true,
    });
    expect(modes).toEqual([PANE_APP_MODE, "_listing", "claude"]);
  });

  test("no lone HTML page: `app` is offered but keeps the registry's rank", () => {
    const modes = paneModeList({
      templates: dirTemplates(["_listing", "app", "claude"]),
      conditions: verdicts(["app", "claude"]),
      isDir: true,
      self: false,
      hasApp: false,
    });
    expect(modes).toEqual(["_listing", "app", "claude"]);
    expect(modes).not.toContain(PANE_APP_MODE);
  });
});

// The pane inherits lib/mode-visibility wholesale: an entry hides ONLY on an
// explicit `false`. This replaces the pane's former fail-closed filter, whose
// posture emptied the menu whenever the verdict call failed — and a menu of one
// hides itself, so a failing gate probe made the whole mode control vanish
// while other surfaces still showed the gated mode. CT-12's own fail-closed
// rule is untouched: a gate that cannot decide answers `false` on the SERVER,
// and an explicit `false` still hides here.
describe("paneModeList — gate visibility is the shared policy", () => {
  test("verdicts in flight are pending, not denied", () => {
    const templates = dirTemplates(["_listing", "zarr_aoi"]);
    const modes = paneModeList({ templates, conditions: null, isDir: true, self: false, hasApp: false });
    expect(modes).toEqual(["_listing", "zarr_aoi"]);
  });

  test("a failed verdict call (no key for the mode) still offers the mode", () => {
    const templates = dirTemplates(["_listing", "zarr_aoi"]);
    const modes = paneModeList({ templates, conditions: {}, isDir: true, self: false, hasApp: false });
    expect(modes).toEqual(["_listing", "zarr_aoi"]);
  });

  test("an explicit denial hides the mode", () => {
    const templates = dirTemplates(["_listing", "zarr_aoi"]);
    const modes = paneModeList({
      templates,
      conditions: verdicts([], ["zarr_aoi"]),
      isDir: true,
      self: false,
      hasApp: false,
    });
    expect(modes).toEqual(["_listing"]);
  });
});

test("the pane's own sentinel is never sent to the server", () => {
  // KNOWN_SENTINEL_MODES is the set the shell will build a render URL for
  // (PT-12). `_app` is pane-local: the pane renders it itself.
  expect(KNOWN_SENTINEL_MODES.has(PANE_APP_MODE)).toBe(false);
});
