import { describe, expect, test } from "bun:test";
import type { TemplateEntry } from "@platform/lib/api";
import {
  DEFAULT_PANE_SIDE,
  PANE_SIDE_MODES,
  PANE_SIDE_OFF,
  activePaneSide,
  paneKey,
  paneSideList,
  paneSideParam,
  paneSideTarget,
  parsePaneSide,
  type PaneSide,
} from "./pane-side";

const entry = (mode: string): TemplateEntry => ({
  mode,
  path: `/t/${mode}/template.html`,
  icon: `/t/${mode}/icon.svg`,
  conditional: true,
});

const NONE = { claude: null, git: null };
const BOTH = { claude: entry("claude"), git: entry("git") };

test("the three, in the order the switcher shows them", () => {
  expect(PANE_SIDE_MODES).toEqual(["preview", "claude", "git"]);
  expect(DEFAULT_PANE_SIDE).toBe("preview");
});

// WHAT AN ABSENT `_side` MEANS, which is the one place the folder's reading of the
// param deliberately differs from the file view's — see parsePaneSide.
describe("parsePaneSide", () => {
  test("no param at all is OPEN at Preview, not closed", () => {
    // Every folder URL, bookmark and recent from before this param existed says
    // nothing, and every one of them means the two-column folder view.
    expect(parsePaneSide(null)).toEqual({ open: true, mode: "preview" });
  });

  test("`off` is the only closed state", () => {
    expect(parsePaneSide(PANE_SIDE_OFF)).toEqual({ open: false, mode: "preview" });
  });

  test("a named mode opens the pane on it", () => {
    expect(parsePaneSide("claude")).toEqual({ open: true, mode: "claude" });
    expect(parsePaneSide("git")).toEqual({ open: true, mode: "git" });
    expect(parsePaneSide("preview")).toEqual({ open: true, mode: "preview" });
  });

  test("an unknown value falls back silently, pane still open", () => {
    // A hand-typed mode, or a `history` carried in from a file view — the router
    // keeps `_side` across a directory hop. Same silent fallback an unknown
    // `_mode` gets, and NOT a closed pane: a value nobody recognises must not
    // take the folder view's other half away.
    expect(parsePaneSide("history")).toEqual({ open: true, mode: "preview" });
    expect(parsePaneSide("graph")).toEqual({ open: true, mode: "preview" });
    expect(parsePaneSide("")).toEqual({ open: true, mode: "preview" });
  });
});

// Round-tripping, and which state gets the CLEAN url.
describe("paneSideParam", () => {
  test("open at the default writes nothing", () => {
    expect(paneSideParam({ open: true, mode: "preview" })).toBeNull();
  });

  test("open on a companion writes the mode", () => {
    expect(paneSideParam({ open: true, mode: "claude" })).toBe("claude");
    expect(paneSideParam({ open: true, mode: "git" })).toBe("git");
  });

  test("closed records only that it is closed, never the mode", () => {
    // The mode to reopen on is session state (Listing), so a shared link to a
    // one-column listing does not carry a companion nobody can see.
    expect(paneSideParam({ open: false, mode: "git" })).toBe(PANE_SIDE_OFF);
    expect(paneSideParam({ open: false, mode: "preview" })).toBe(PANE_SIDE_OFF);
  });

  test("every writable state parses back to itself, modulo the shut mode", () => {
    for (const mode of PANE_SIDE_MODES) {
      const open = { open: true, mode };
      expect(parsePaneSide(paneSideParam(open))).toEqual(open);
      expect(parsePaneSide(paneSideParam({ open: false, mode })).open).toBe(false);
    }
  });
});

// Which of the three a folder actually offers. `preview` always — it is the pane's
// identity, and a row with no template at all still has the metadata card.
describe("paneSideList", () => {
  test("a folder with neither companion offers Preview alone", () => {
    // A mount-backed folder: both gates refuse a mount, so the pill hides itself
    // and the pane is what it was before the split.
    expect(paneSideList(NONE)).toEqual(["preview"]);
  });

  test("a folder in a repository offers all three, in order", () => {
    expect(paneSideList(BOTH)).toEqual(["preview", "claude", "git"]);
  });

  test("a folder outside a repository loses Git and keeps Claude", () => {
    expect(paneSideList({ claude: entry("claude"), git: null })).toEqual([
      "preview",
      "claude",
    ]);
  });
});

// A request for a mode this folder hasn't got falls back — and the param is left
// alone by the caller, so hopping out of a repository and back in does not reset
// the pane.
describe("activePaneSide", () => {
  test("an offered request wins", () => {
    expect(activePaneSide(paneSideList(BOTH), "git")).toBe("git");
    expect(activePaneSide(paneSideList(BOTH), "claude")).toBe("claude");
  });

  test("an unavailable request lands on Preview", () => {
    expect(activePaneSide(paneSideList(NONE), "git")).toBe("preview");
    expect(activePaneSide(paneSideList({ claude: entry("claude"), git: null }), "git")).toBe(
      "preview"
    );
  });
});

// The pane's REMOUNT identity, and the one thing it has to get right: Git is about
// the FOLDER, so its key must not move with the selection.
describe("paneKey", () => {
  const folder = "/w/repo";

  test("Git ignores the selection entirely", () => {
    // Arrow-keying down a listing must not reload a `git status` per keystroke.
    const a = paneKey("git", folder, "/w/repo/a.md", 1);
    const b = paneKey("git", folder, "/w/repo/b.md", 1);
    const none = paneKey("git", folder, null, 0);
    const many = paneKey("git", folder, null, 3);
    expect(a).toBe(b);
    expect(a).toBe(none);
    expect(a).toBe(many);
  });

  test("Claude follows the selected row, and falls back to the folder", () => {
    expect(paneKey("claude", folder, "/w/repo/a.md", 1)).not.toBe(
      paneKey("claude", folder, "/w/repo/b.md", 1)
    );
    // Nothing selected and a multi-selection are both "the folder itself".
    expect(paneKey("claude", folder, null, 0)).toBe(paneKey("claude", folder, null, 4));
  });

  test("Preview keeps its three selection states apart", () => {
    const row = paneKey("preview", folder, "/w/repo/a.md", 1);
    const self = paneKey("preview", folder, null, 0);
    const many = paneKey("preview", folder, null, 3);
    expect(new Set([row, self, many]).size).toBe(3);
  });

  test("the modes never collide on one key", () => {
    const keys = (PANE_SIDE_MODES as readonly PaneSide[]).map((m) =>
      paneKey(m, folder, "/w/repo/a.md", 1)
    );
    expect(new Set(keys).size).toBe(keys.length);
  });
});

// Three modes, two subjects.
describe("paneSideTarget", () => {
  const folder = "/w/repo";

  test("Git is aimed at the folder, whatever is selected", () => {
    expect(paneSideTarget("git", folder, "/w/repo/a.md")).toBe(folder);
    expect(paneSideTarget("git", folder, null)).toBe(folder);
  });

  test("the other two are aimed at the row, and at the folder without one", () => {
    expect(paneSideTarget("claude", folder, "/w/repo/a.md")).toBe("/w/repo/a.md");
    expect(paneSideTarget("preview", folder, "/w/repo/a.md")).toBe("/w/repo/a.md");
    expect(paneSideTarget("claude", folder, null)).toBe(folder);
  });
});
