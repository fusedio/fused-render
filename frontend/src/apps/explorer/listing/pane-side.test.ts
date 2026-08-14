import { describe, expect, test } from "bun:test";
import type { TemplateEntry } from "@platform/lib/api";
import {
  DEFAULT_PANE_SIDE,
  PANE_SIDE_MODES,
  PANE_SIDE_OFF,
  activePaneSide,
  paneKey,
  paneSideIconEntry,
  paneSideList,
  paneSideMenu,
  paneSideParam,
  paneSideTarget,
  parsePaneSide,
  resumingPaneSession,
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
    // A hand-typed mode, or a stale `_side` carried in from a file view — the
    // router keeps `_side` across a directory hop. Same silent fallback an
    // unknown `_mode` gets, and NOT a closed pane: a value nobody recognises must
    // not take the folder view's other half away.
    expect(parsePaneSide("notes")).toEqual({ open: true, mode: "preview" });
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
    // A mount-backed folder: both gates refuse a mount, so the pane can only ever
    // BE Preview. The switcher still lists all three (paneSideMenu) — this list
    // is what the pane may show, not what the header contains.
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

// WHAT THE SWITCHER DRAWS, which is all three whatever the folder offers — the
// folder half of the file sidebar's rule. An unofferable mode is a disabled row
// carrying its reason, so the header holds still as the user walks from a
// repository into a folder outside one instead of shedding pills (and, at one
// pill, hiding the control altogether).
describe("paneSideMenu", () => {
  // Copy the user reads, so it is written out here rather than re-derived from
  // the constant it is testing.
  const NO_REPO = "Not inside a git repository";
  const NO_CLAUDE = "Claude is not available for this file";
  const rows = (e: Parameters<typeof paneSideMenu>[0]) =>
    paneSideMenu(e).map((r) => [r.mode, r.disabledReason ?? (r.pending ? "…" : null)]);

  test("a folder in a repository offers all three, none explained", () => {
    expect(rows(BOTH)).toEqual([
      ["preview", null],
      ["claude", null],
      ["git", null],
    ]);
  });

  test("a folder outside a repository keeps Git, disabled and explained", () => {
    expect(rows({ claude: entry("claude"), git: null })).toEqual([
      ["preview", null],
      ["claude", null],
      ["git", NO_REPO],
    ]);
  });

  test("a mount-backed folder still gets a switcher", () => {
    // Both gates refuse a mount. This used to be a one-pill menu, which hid
    // itself, leaving the pane header a lone chevron.
    expect(rows(NONE)).toEqual([
      ["preview", null],
      ["claude", NO_CLAUDE],
      ["git", NO_REPO],
    ]);
  });

  test("an undecided probe spins rather than claiming a reason", () => {
    // "Not inside a git repository" before anyone has looked is a guess, and one
    // that flips to a working Git pill a moment later.
    expect(rows({ claude: null, git: null, claudePending: true, gitPending: true })).toEqual([
      ["preview", null],
      ["claude", "…"],
      ["git", "…"],
    ]);
    // The two probes land independently: a settled denial beside an open probe
    // is the usual frame, and each row says only what is known of IT.
    expect(rows({ claude: null, git: null, gitPending: true })).toEqual([
      ["preview", null],
      ["claude", NO_CLAUDE],
      ["git", "…"],
    ]);
  });

  test("Preview is never explained away", () => {
    // It is the pane's identity — it cannot be unavailable, so it never carries
    // a reason and is always selectable.
    for (const e of [NONE, BOTH, { claude: null, git: null, gitPending: true }])
      expect(paneSideMenu(e)[0]).toEqual({ mode: "preview" });
  });

  test("the rows decide nothing", () => {
    // What the pane may BE is still paneSideList's answer: a disabled row must
    // not become a mode the pane can land on.
    const outside = { claude: entry("claude"), git: null };
    expect(paneSideMenu(outside).length).toBe(3);
    expect(paneSideList(outside)).toEqual(["preview", "claude"]);
    expect(activePaneSide(paneSideList(outside), "git")).toBe("preview");
  });
});

// WHERE A ROW'S ICON COMES FROM. A disabled row is the mode with the click taken
// away, so it wears the mode's own glyph — never a generic one, and never
// Preview's (two identical icons in a three-row menu read as a duplicate entry).
describe("paneSideIconEntry", () => {
  const git = entry("git");

  test("an offered mode uses its own entry", () => {
    expect(paneSideIconEntry("git", BOTH)).toBe(BOTH.git);
    expect(paneSideIconEntry("claude", BOTH)).toBe(BOTH.claude);
  });

  test("a disabled mode falls back to the binding the gate refused", () => {
    // A folder outside a repository: nothing to frame, but `git` is bound and its
    // icon exists — dir-mode keeps the entry through the denial for this.
    expect(paneSideIconEntry("git", { claude: null, git: null, gitBound: git })).toBe(git);
  });

  test("a mode bound nowhere has no icon to offer", () => {
    // The caller's last resort, and only here.
    expect(paneSideIconEntry("git", NONE)).toBe(null);
    expect(paneSideIconEntry("claude", NONE)).toBe(null);
  });

  test("the offered entry always outranks the binding", () => {
    expect(paneSideIconEntry("git", { claude: null, git, gitBound: entry("stale") })).toBe(git);
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

// Arriving to CONTINUE one conversation (the Inbox's "Open in explorer"). What
// hangs off the answer is the listing's one-shot auto-select, which spends its
// shot instead of firing — because the target it would pick (a row) is the one
// target a session id cannot be resolved against. See pane-side.ts.
describe("resumingPaneSession", () => {
  const id = "c0f1bb8a-2fcb-4e4f-a3d2-390b3aef8afc";

  test("both halves of the inbox link, in either order", () => {
    expect(resumingPaneSession(`?_side=claude&session_id=${id}`)).toBe(true);
    expect(resumingPaneSession(`?session_id=${id}&_side=claude`)).toBe(true);
    // and undisturbed by the params it travels with
    expect(resumingPaneSession(`?snapshot=1&_side=claude&session_id=${id}&sort=size`)).toBe(true);
  });

  test("one half alone is not an arrival", () => {
    // A pane the user simply left open on the chat: nothing here says WHICH
    // conversation, so the row-driven target is still the right one.
    expect(resumingPaneSession("?_side=claude")).toBe(false);
    // And a session id under another mode is not this pane's business — Git and
    // Preview have their own subjects, neither of them a conversation.
    expect(resumingPaneSession(`?_side=git&session_id=${id}`)).toBe(false);
    expect(resumingPaneSession(`?session_id=${id}`)).toBe(false);
  });

  test("an empty search, and an empty id, are both no", () => {
    expect(resumingPaneSession("")).toBe(false);
    expect(resumingPaneSession("?")).toBe(false);
    // `session_id=` is what the chat template writes when it CLEARS the param
    // (`{ default: "" }`), so a present-but-blank id must not read as a resume.
    expect(resumingPaneSession("?_side=claude&session_id=")).toBe(false);
  });
});
