import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import type { TemplateEntry } from "@platform/lib/api";
import {
  activePaneMode,
  paneChatOnly,
  paneModeList,
  paneOpenAction,
  paneOpenTarget,
} from "./pane-modes";

// The universal `/` directory key as the built-in registry ships it (SPEC
// PT-13): **`claude` first** (D277) with `_listing` behind it, and every peer
// except the `_listing` sentinel condition.py-gated.
function dirTemplates(modes: string[] = ["claude", "_listing", "git", "graph"]): TemplateEntry[] {
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
  test("a selected subfolder defaults to CLAUDE, the registry's lead", () => {
    // D277: the pane reads the list for its lead (`activePaneMode` with no
    // override), so the registry's order IS the pane's default, and a folder now
    // opens the chat about itself rather than running anything of the folder's.
    const modes = paneModeList({
      templates: dirTemplates(),
      conditions: verdicts(["claude", "git"], ["graph"]),
      isDir: true,
    });
    expect(modes).toEqual(["claude", "_listing", "git"]);
    expect(activePaneMode(modes, null)).toBe("claude");
  });

  test("a folder whose claude gate says NO falls back to the listing", () => {
    // The deliberate answer to "what if the default is denied" (D277): the next
    // mode in the registry's order, which is the unconditional `_listing`
    // sentinel — the embedded peek. It renders no template and runs no Python,
    // so the denial can never fall through to something heavier than the default.
    const modes = paneModeList({
      templates: dirTemplates(),
      conditions: verdicts(["git"], ["claude", "graph"]),
      isDir: true,
    });
    expect(modes).toEqual(["_listing", "git"]);
    expect(activePaneMode(modes, null)).toBe("_listing");
  });

  test("a folder holding a lone page is still just a folder", () => {
    // It used to lead with a pane-only `_app` sentinel rendering that page in
    // place (D264 deleted that), and then with the page itself through a retarget
    // (D269, deleted by D277). A folder's modes are the FOLDER's, and the page it
    // holds is one row of the listing behind them.
    const modes = paneModeList({
      templates: dirTemplates(["claude", "_listing"]),
      conditions: verdicts(["claude"]),
      isDir: true,
    });
    expect(modes).toEqual(["claude", "_listing"]);
    expect(activePaneMode(modes, null)).toBe("claude");
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

// A SELECTED FOLDER IS PREVIEWED AS A FOLDER — never as the page it holds
// (D277, deleting D269's pane half). The pane used to resolve a folder with a
// top-level `.html` to that page and preview it as a FILE, which meant selecting
// a row in the listing RAN the folder's app: its template's Python, its buttons,
// its network calls, for a folder the user had merely highlighted.
//
// The component needs a React renderer this setup does not have, so the absence
// is pinned at its source. Both halves matter: the entry rule must not be
// consulted, and the retarget state it fed must be gone — a live `entryRow`
// would put the page back through the same `view` even if the helper were
// reached some other way.
// The second door onto the same page, and the reason `paneChatOnly` exists.
//
// The `claude` template has a preview pane OF ITS OWN, and for a FOLDER it fills
// that pane by resolving the folder's entry page and rendering it
// (templates/shared/app_entry.py). So making `claude` a folder's pane default
// without taking that pane away would put the app page back on screen — nested
// one level deeper, running the same Python, for the same mere selection.
// `chat_only=1` is what removes it, and the claude template checks the flag
// BEFORE it looks an entry page up at all.
describe("paneChatOnly", () => {
  test("the claude template never gets a pane of its own in here", () => {
    expect(paneChatOnly("claude")).toBe(true);
  });

  test("nothing else is affected", () => {
    // `git`'s subject is the folder and it has no preview pane to take away;
    // `_listing` builds no /render URL at all, and a file's own modes are
    // ordinary templates.
    expect(paneChatOnly("git")).toBe(false);
    expect(paneChatOnly("_listing")).toBe(false);
    expect(paneChatOnly("_render")).toBe(false);
    expect(paneChatOnly("duckdb")).toBe(false);
  });
});

describe("the pane never previews a folder as its app page", () => {
  const src = readFileSync(
    join(import.meta.dir, "../ListingPreviewPane.tsx"),
    "utf8",
  );

  test("the pane's one chat-only rule serves both its claude surfaces", () => {
    // The pane's `claude` SIDE has always passed the flag; the row-mode embed
    // must pass it too, and both from this decision — one query literal, spelled
    // once as a constant, and two callers asking whether to send it.
    expect(src.match(/&chat_only=1/g)?.length).toBe(1);
    expect(src.match(/paneChatOnly\(/g)?.length).toBe(2);
  });

  test("the folder-entry rule is not consulted for a selected row", () => {
    // `lib/app-entry.ts` is deleted outright — the pane was its only caller —
    // so this holds today by there being nothing to import. It is pinned anyway,
    // because the rule still LIVES on the server (`app_listing.app_entry`) and in
    // the claude template (`templates/shared/app_entry.py`): re-deriving it here
    // is a two-line change, and the pin is what makes that change fail loudly.
    // The comments may still name it; the import and the call are what count.
    expect(src).not.toMatch(/^import .*app-entry/m);
    expect(src).not.toContain("entryHtmlPath(");
  });

  test("no retarget state stands between the selected row and the preview", () => {
    expect(src).not.toContain("entryRow");
    expect(src).not.toContain("resolvingEntry");
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
