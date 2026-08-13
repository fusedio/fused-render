import { describe, expect, test } from "bun:test";
import type { TemplateEntry } from "@platform/lib/api";
import {
  activePaneMode,
  paneModeList,
  paneOpenAction,
  paneOpenTarget,
} from "./pane-modes";

// The universal `/` directory key as the built-in registry ships it (SPEC
// PT-13): `_listing` first and unconditional, every peer condition.py-gated.
function dirTemplates(modes: string[] = ["_listing", "app", "claude", "git", "graph"]): TemplateEntry[] {
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

// The pane's list is only ever built for a SELECTED row. The self target
// (nothing selected — the folder already open on the left) never reaches this
// module: it shows no mode picker and always renders the neutral hint, so
// there is nothing to rank. The rules that used to live here for it — drop
// `_listing`, land on no mode unless the folder had an app of its own — are
// gone with the picker.
describe("paneModeList — the selected target", () => {
  test("a selected subfolder still defaults to the embedded listing", () => {
    const modes = paneModeList({
      templates: dirTemplates(),
      conditions: verdicts(["claude", "git"], ["app", "graph"]),
      isDir: true,
    });
    expect(modes[0]).toBe("_listing");
    expect(activePaneMode(modes, null)).toBe("_listing");
  });

  test("a folder holding a lone page is still just a folder", () => {
    // It used to lead with a pane-only `_app` sentinel rendering that page in
    // place. The app concept is gone (D264): every folder previews as its
    // listing, and the page is one row of it.
    const modes = paneModeList({
      templates: dirTemplates(["_listing", "claude"]),
      conditions: verdicts(["claude"]),
      isDir: true,
    });
    expect(modes).toEqual(["_listing", "claude"]);
  });

  test("a file drops `_listing` and takes its own first mode", () => {
    const templates = [
      { mode: "_render", path: null, icon: null },
      { mode: "code", path: "/t/code/template.html", icon: null },
      { mode: "_listing", path: null, icon: null },
    ] as TemplateEntry[];
    const modes = paneModeList({ templates, conditions: {}, isDir: false });
    expect(modes).toEqual(["_render", "code"]);
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
    const modes = paneModeList({ templates, conditions: null, isDir: true });
    expect(modes).toEqual(["_listing", "zarr_aoi"]);
  });

  test("a failed verdict call (no key for the mode) still offers the mode", () => {
    const templates = dirTemplates(["_listing", "zarr_aoi"]);
    const modes = paneModeList({ templates, conditions: {}, isDir: true });
    expect(modes).toEqual(["_listing", "zarr_aoi"]);
  });

  test("an explicit denial hides the mode", () => {
    const templates = dirTemplates(["_listing", "zarr_aoi"]);
    const modes = paneModeList({
      templates,
      conditions: verdicts([], ["zarr_aoi"]),
      isDir: true,
    });
    expect(modes).toEqual(["_listing"]);
  });
});

// `null` from activePaneMode has ONE cause now: an empty list. It used to
// have two — the self target's "nothing previewed" state was also null — and
// conflating them cost a regression, the pane serving the self hint to a
// SELECTED row whose every template was gate-denied and stealing that row's
// metadata card. The self target no longer resolves a mode at all, so the
// overload is gone rather than guarded.
test("an empty list is the only way to have no active mode", () => {
  // A file that maps to nothing, or whose every template is gate-denied.
  expect(activePaneMode([], null)).toBeNull();
  // Anything offered is a default, whatever it is.
  expect(activePaneMode(["claude"], null)).toBe("claude");
  // An override wins while it is offered, and is ignored when it is not.
  expect(activePaneMode(["claude", "git"], "git")).toBe("git");
  expect(activePaneMode(["claude"], "git")).toBe("claude");
});

// The expand icon opens what the pane is SHOWING, not what the row defaults to
// — the whole point of "make this the whole view".
describe("paneOpenTarget", () => {
  const file = { path: "/w/notes.md", isDir: false };
  const dir = { path: "/w/proj", isDir: true };

  test("a template mode is carried into the full-screen open", () => {
    expect(paneOpenTarget(file, "claude")).toEqual({
      path: "/w/notes.md",
      isDir: false,
      mode: "claude",
    });
    // `_render` is a real sentinel the full-screen view understands (PT-12),
    // so it travels like any other mode.
    expect(paneOpenTarget(file, "_render")).toEqual({
      path: "/w/notes.md",
      isDir: false,
      mode: "_render",
    });
  });

  test("a folder shown as its listing opens plainly", () => {
    // `_mode=_listing` would be the destination's own default written out
    // longhand — Preview strips it again on the next mode switch anyway.
    expect(paneOpenTarget(dir, "_listing")).toEqual({ path: "/w/proj", isDir: true });
  });

  test("nothing offered means nothing to carry", () => {
    expect(paneOpenTarget(file, null)).toEqual({ path: "/w/notes.md", isDir: false });
  });
});

// Which control the pane's header offers for the previewed row. "Expand" means
// something for a FILE and nothing for a folder — a folder full-screen is just
// its listing, which is already on the left of the very split the button sits
// in.
describe("paneOpenAction", () => {
  const file = { path: "/w/notes.md", isDir: false };
  const dir = { path: "/w/proj", isDir: true };

  test("a file expands, in the mode the pane is showing", () => {
    expect(paneOpenAction(file, "claude")).toEqual({
      kind: "expand",
      target: { path: "/w/notes.md", isDir: false, mode: "claude" },
    });
  });

  test("a folder offers nothing here, whatever mode it is showing", () => {
    // An expand control on a folder would open the folder's listing, which is
    // what the left half of this very split already shows.
    for (const mode of ["_listing", "claude", "git", null]) {
      expect(paneOpenAction(dir, mode)).toEqual({ kind: "none" });
    }
  });
});
