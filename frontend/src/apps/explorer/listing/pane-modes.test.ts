import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import type { TemplateEntry } from "@platform/lib/api";
import { paneChatOnly, paneModeList } from "./pane-modes";

// The universal `/` directory key as the built-in registry ships it (SPEC
// PT-13): **`claude` first** (D280) with `_listing` behind it, and every peer
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
    // D280: the pane reads the list for its lead (its first entry, with no
    // override), so the registry's order IS the pane's default, and a folder now
    // opens the chat about itself rather than running anything of the folder's.
    // (The row-mode resolution that used to be pinned as a second call here —
    // `activePaneMode` — is deleted with the rest of the row-mode machinery,
    // D460; `modes[0]` is the whole of what it ever did with a null override.)
    const modes = paneModeList({
      templates: dirTemplates(),
      conditions: verdicts(["claude", "git"], ["graph"]),
      isDir: true,
    });
    expect(modes).toEqual(["claude", "_listing", "git"]);
    expect(modes[0]).toBe("claude");
  });

  test("a folder whose claude gate says NO falls back to the listing", () => {
    // The deliberate answer to "what if the default is denied" (D280): the next
    // mode in the registry's order, which is the unconditional `_listing`
    // sentinel — the embedded peek. It renders no template and runs no Python,
    // so the denial can never fall through to something heavier than the default.
    const modes = paneModeList({
      templates: dirTemplates(),
      conditions: verdicts(["git"], ["claude", "graph"]),
      isDir: true,
    });
    expect(modes).toEqual(["_listing", "git"]);
    expect(modes[0]).toBe("_listing");
  });

  test("a folder holding a lone page is still just a folder", () => {
    // It used to lead with a pane-only `_app` sentinel rendering that page in
    // place (D264 deleted that), and then with the page itself through a retarget
    // (D269, deleted by D280). A folder's modes are the FOLDER's, and the page it
    // holds is one row of the listing behind them.
    const modes = paneModeList({
      templates: dirTemplates(["claude", "_listing"]),
      conditions: verdicts(["claude"]),
      isDir: true,
    });
    expect(modes).toEqual(["claude", "_listing"]);
    expect(modes[0]).toBe("claude");
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
// (D280, deleting D269's pane half). The pane used to resolve a folder with a
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
// (templates/shared/app_entry.py). While the pane still had a row MODE that
// could resolve to `claude` (a selected folder's default view), making that
// mode's default without taking the template's own pane away would have put
// the app page back on screen — nested one level deeper, running the same
// Python, for the same mere selection. `chat_only=1` is what removes it, and
// the claude template checks the flag BEFORE it looks an entry page up at all.
// D460 deleted that row mode, but the `claude` COMPANION iframe is the exact
// same template rendered the exact same way, so the flag is still required.
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

  test("the pane's chat-only rule still guards its one claude surface", () => {
    // D460 deleted the row-mode embed entirely (the pane no longer previews a
    // selected row's own templates at all), so the `claude` COMPANION iframe
    // is the only caller left — one query literal, spelled once as a constant,
    // one call asking whether to send it.
    expect(src.match(/&chat_only=1/g)?.length).toBe(1);
    expect(src.match(/paneChatOnly\(/g)?.length).toBe(1);
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
