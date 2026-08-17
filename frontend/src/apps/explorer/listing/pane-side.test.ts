import { describe, expect, test } from "bun:test";
import type { TemplateEntry } from "@platform/lib/api";
import {
  PANE_SIDE_COMPANIONS,
  PANE_SIDE_FALLBACK,
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

test("two companions in switcher order, and a fallback that is not one of them", () => {
  // D285: the pane is a companion column. `PANE_SIDE_MODES` still carries `preview`
  // because the pane can BE in that state, but only as the neither-companion
  // fallback — `PANE_SIDE_COMPANIONS` is what a user can choose and what the URL can
  // carry, and the old `DEFAULT_PANE_SIDE` is gone with the idea that `preview` was
  // anything's default.
  expect(PANE_SIDE_MODES).toEqual(["preview", "claude", "git"]);
  expect(PANE_SIDE_COMPANIONS).toEqual(["claude", "git"]);
  expect(PANE_SIDE_FALLBACK).toBe("preview");
});

// WHAT AN ABSENT `_side` MEANS, which is the one place the folder's reading of the
// param deliberately differs from the file view's — see parsePaneSide.
describe("parsePaneSide", () => {
  test("no param at all is OPEN with NO CHOICE, not closed and not Preview", () => {
    // Every folder URL, bookmark and recent from before this param existed says
    // nothing, and every one of them means the two-column folder view. What it does
    // NOT mean any more is `preview` (D285): absence is `mode: null`, "no choice
    // yet", which `activePaneSide` resolves against what the folder offers — so a
    // clean URL lands on Claude instead of on a mode nothing offers.
    expect(parsePaneSide(null)).toEqual({ open: true, mode: null });
  });

  test("`off` is the only closed state", () => {
    // A shut pane records no mode either — it reopens at whatever is offered first,
    // which since D285 is a companion rather than Preview.
    expect(parsePaneSide(PANE_SIDE_OFF)).toEqual({ open: false, mode: null });
  });

  test("a named COMPANION opens the pane on it — and `preview` is not one", () => {
    expect(parsePaneSide("claude")).toEqual({ open: true, mode: "claude" });
    expect(parsePaneSide("git")).toEqual({ open: true, mode: "git" });
    // `preview` used to round-trip here. D285 refuses it like any unknown value: it
    // names a state the pane can only FALL into, so honouring it would pin the pane
    // to a hint the switcher cannot get the user out of.
    expect(parsePaneSide("preview")).toEqual({ open: true, mode: null });
  });

  test("an unknown value falls back silently, pane still open", () => {
    // A hand-typed mode, or a stale `_side` carried in from a file view — the
    // router keeps `_side` across a directory hop. Same silent fallback an
    // unknown `_mode` gets, and NOT a closed pane: a value nobody recognises must
    // not take the folder view's other half away.
    expect(parsePaneSide("notes")).toEqual({ open: true, mode: null });
    expect(parsePaneSide("graph")).toEqual({ open: true, mode: null });
    expect(parsePaneSide("")).toEqual({ open: true, mode: null });
  });
});

// Round-tripping, and which state gets the CLEAN url.
describe("paneSideParam", () => {
  test("open with NO CHOICE writes nothing — the clean URL (PT-9)", () => {
    // This case read `mode: "preview"` until D285, when "the default" stopped being a
    // named mode. The clean URL now means "no choice recorded", and it resolves to
    // whichever companion the folder offers first — so Claude is what a clean folder
    // link lands on. Listing's `selectSide` normalises a pick of the leading
    // companion back to null for exactly this reason: clicking the mode you are
    // already on must not grow a param on every shared listing link.
    expect(paneSideParam({ open: true, mode: null })).toBeNull();
  });

  test("open on a companion writes the mode", () => {
    expect(paneSideParam({ open: true, mode: "claude" })).toBe("claude");
    expect(paneSideParam({ open: true, mode: "git" })).toBe("git");
  });

  test("closed records only that it is closed, never the mode", () => {
    // The mode to reopen on is session state (Listing), so a shared link to a
    // one-column listing does not carry a companion nobody can see.
    expect(paneSideParam({ open: false, mode: "git" })).toBe(PANE_SIDE_OFF);
    expect(paneSideParam({ open: false, mode: null })).toBe(PANE_SIDE_OFF);
  });

  test("every writable state parses back to itself, modulo the shut mode", () => {
    // The writable states are the COMPANIONS plus no-choice (D285) — `preview` is not
    // among them, which is the round-trip property that makes the fallback
    // unreachable from a URL.
    for (const mode of [...PANE_SIDE_COMPANIONS, null] as const) {
      const open = { open: true, mode };
      expect(parsePaneSide(paneSideParam(open))).toEqual(open);
      expect(parsePaneSide(paneSideParam({ open: false, mode })).open).toBe(false);
    }
  });
});

// WHAT THE PANE MAY BE ON, and it no longer depends on the subject at all (D285):
// `preview` is not offered for a file row either, so the two describes that used to
// stand here — one for a file row or multi-selection, one for a folder subject — have
// collapsed into this one. The `subjectIsDir` flag they were parameterised over is
// deleted, and with it the only reason this function ever asked about the row.
//
// The old file-row premise, "`preview` always — it is the pane's identity there",
// is exactly what D285 deleted: the pane is a companion column, the shape the
// full-screen file sidebar has had all along.
describe("paneSideList", () => {
  test("the companions alone, whatever the subject, and Claude is what it lands on", () => {
    expect(paneSideList(BOTH)).toEqual(["claude", "git"]);
    // An absent `_side` parses as no choice (`null`), and the resolve lands on the
    // first offered side.
    expect(activePaneSide(paneSideList(BOTH), null)).toBe("claude");
  });

  test("an explicit Git choice still wins", () => {
    // The user's own `?_side=git` is a choice, not a default.
    expect(activePaneSide(paneSideList(BOTH), "git")).toBe("git");
  });

  test("a folder outside a repository lands on Claude with Git gone", () => {
    const outside = { claude: entry("claude"), git: null };
    expect(paneSideList(outside)).toEqual(["claude"]);
    expect(activePaneSide(paneSideList(outside), "git")).toBe("claude");
  });

  // A PROBE STILL OUT IS NOT A DENIAL, and conflating the two put the reported bug
  // straight back for the window after a folder opens. `Listing` nulls both entries
  // while `lib/dir-mode` is still resolving them (a stat plus a per-gate
  // condition.py fork), so "no companion offered" is the same shape as
  // mount-backed — and answering `preview` there made the pill read "Preview" while
  // a chat rendered inside it, then flipped the side when the probe landed and spawned
  // `agent.py` a SECOND time. So an undecided subject offers NOTHING and the pane
  // holds a skeleton.
  const PENDING = { claude: null, git: null, claudePending: true, gitPending: true };

  test("probes still out offers nothing yet — for every row type now", () => {
    expect(paneSideList(PENDING)).toEqual([]);
    // The contract the caller reads: an EMPTY list means undecided, and the pane
    // must not render a mode. `preview` in particular is not the answer.
    expect(paneSideList(PENDING)).not.toContain("preview");
  });

  test("one probe landing is enough to decide", () => {
    // Git still out, Claude answered: the chat is offered and the pane can mount.
    // Nothing Git's verdict can say will outrank it, so there is nothing to wait for.
    expect(paneSideList({ ...PENDING, claude: entry("claude"), claudePending: false }))
      .toEqual(["claude"]);
    // Claude denied, Git answered: the first side ON OFFER, which is Git.
    expect(paneSideList({ ...PENDING, git: entry("git"), gitPending: false, claudePending: false }))
      .toEqual(["git"]);
  });

  // …EXCEPT WHEN THE ONE THAT LANDED IS THE FOLLOWER. The two probes are independent
  // `useDirMode` calls with their own caches, so Git answering first is ordinary, and
  // `activePaneSide` tracks the live list: the pane opened on Git and then JUMPED to
  // Claude when its probe landed, because Claude leads the order. Same defect as the
  // file sidebar's (lib/preview-side's `defaultSide`) and fixed by the same rule —
  // the LEADER decides, and while the leader is undecided so is the pane.
  test("Git landing first does not open a pane that Claude would then displace", () => {
    const gitFirst = { ...PENDING, git: entry("git"), gitPending: false };
    expect(paneSideList(gitFirst)).toEqual([]);
    // ...so there is nothing on screen to move: the caller holds its skeleton on an
    // empty list, and `activePaneSide`'s answer is only the pill's placeholder.
    expect(activePaneSide(paneSideList(gitFirst), null)).toBe(PANE_SIDE_FALLBACK);
    // When Claude lands, BOTH are offered — that list is what the pane may be on —
    // and the pane opens on the leader, once.
    const both = { ...gitFirst, claude: entry("claude"), claudePending: false };
    expect(paneSideList(both)).toEqual(["claude", "git"]);
    expect(activePaneSide(paneSideList(both), null)).toBe("claude");
    // ...and a DENIAL still lands the pane on Git, which is a decision, not a swap.
    expect(paneSideList({ ...gitFirst, claudePending: false })).toEqual(["git"]);
    expect(activePaneSide(paneSideList({ ...gitFirst, claudePending: false }), null)).toBe("git");
  });

  test("an explicit request is still honoured through the wait", () => {
    // The undecided list is about what an ABSENT `_side` resolves to; a `?_side=git`
    // deep link is the user's own choice and `activePaneSide` keeps it (the param is
    // deliberately never reconciled away here).
    const gitFirst = { ...PENDING, git: entry("git"), gitPending: false };
    expect(activePaneSide(paneSideList(gitFirst), "git")).toBe(PANE_SIDE_FALLBACK);
    expect(activePaneSide(paneSideList({ ...gitFirst, claudePending: false }), "git")).toBe("git");
  });

  test("a FILE row waits with everything else now — the old exemption is void", () => {
    // This case asserted the opposite: a file row got `["preview"]` while the probes
    // were out, because "its `preview` is its own template list, which this probe says
    // nothing about". D285 removed that offer, so there is nothing to resolve for a
    // file row either until a companion answers. The wait is bounded — `useDirMode` is
    // keyed on the FOLDER and caches per directory, so the window opens once per
    // folder open and arrow-keying between rows afterwards never re-enters it.
    expect(paneSideList(PENDING)).toEqual([]);
  });

  test("with NEITHER companion, `preview` comes back as the fallback", () => {
    // A mount-backed folder: both gates refuse. The pane must show something, and
    // this is the one state that renders what pane-modes.ts resolves — the hint for
    // the folder, a file row's own default template, or the metadata card.
    expect(paneSideList(NONE)).toEqual(["preview"]);
    expect(activePaneSide(paneSideList(NONE), null)).toBe("preview");
    // …and it cannot be REQUESTED, only fallen into: a `_side=preview` never becomes
    // a want in the first place (parsePaneSide refuses it).
    expect(parsePaneSide("preview").mode).toBeNull();
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

  test("a folder in a repository offers both companions, neither explained", () => {
    expect(rows(BOTH)).toEqual([
      ["claude", null],
      ["git", null],
    ]);
  });

  test("a folder outside a repository keeps Git, disabled and explained", () => {
    expect(rows({ claude: entry("claude"), git: null })).toEqual([
      ["claude", null],
      ["git", NO_REPO],
    ]);
  });

  test("a mount-backed folder still gets a switcher", () => {
    // Both gates refuse a mount. This used to be a one-pill menu, which hid
    // itself, leaving the pane header a lone chevron.
    // Two rows, both disabled and both explained. The pane is on its `preview`
    // fallback here, and the menu says so by having nothing selectable — which is a
    // truer account than a Preview row that looks like a choice (D285).
    expect(rows(NONE)).toEqual([
      ["claude", NO_CLAUDE],
      ["git", NO_REPO],
    ]);
  });

  test("an undecided subject draws two spinners", () => {
    // A probe still out is neither offered nor denied, so the companions are CT-12
    // spinners rather than denials.
    const pending = { claude: null, git: null, claudePending: true, gitPending: true };
    expect(paneSideMenu(pending)).toEqual([
      { mode: "claude", pending: true },
      { mode: "git", pending: true },
    ]);
  });

  test("NO subject draws a Preview row — not even the fallback state (D285)", () => {
    // The menu must not offer what a user cannot pick. `preview` is now unpickable
    // everywhere: it is a state the pane falls into, so a row for it would be a
    // control that cannot be honoured, and it carries no `disabledReason` either
    // (it is not unavailable for a reason — it is not a mode).
    expect(paneSideMenu(BOTH).map((r) => r.mode)).toEqual(["claude", "git"]);
    // Unavailable COMPANIONS are still drawn and still explained — that rule is
    // untouched, and it is why the pill never shrinks to one row and hides.
    expect(paneSideMenu({ claude: entry("claude"), git: null })).toEqual([
      { mode: "claude" },
      { mode: "git", disabledReason: NO_REPO },
    ]);
    // Even where the pane IS on `preview` — neither companion offered — the menu
    // shows the two companions and nothing else. This case used to assert the row
    // came back here.
    expect(paneSideMenu(NONE).map((r) => r.mode)).toEqual(["claude", "git"]);
  });

  test("an undecided probe spins rather than claiming a reason", () => {
    // "Not inside a git repository" before anyone has looked is a guess, and one
    // that flips to a working Git pill a moment later.
    expect(rows({ claude: null, git: null, claudePending: true, gitPending: true })).toEqual([
      ["claude", "…"],
      ["git", "…"],
    ]);
    // The two probes land independently: a settled denial beside an open probe
    // is the usual frame, and each row says only what is known of IT.
    expect(rows({ claude: null, git: null, gitPending: true })).toEqual([
      ["claude", NO_CLAUDE],
      ["git", "…"],
    ]);
  });

  test("Preview is never a row, in any state (D285)", () => {
    // This case asserted the opposite — that `preview` is always row 0 and never
    // carries a reason, because "it is the pane's identity". It is not a mode at all
    // now, so the menu never mentions it; what it cannot do is carry a reason, since
    // the reasons belong to companions that are unavailable.
    for (const e of [NONE, BOTH, { claude: null, git: null, gitPending: true }]) {
      expect(paneSideMenu(e).map((r) => r.mode)).toEqual(["claude", "git"]);
    }
  });

  test("the rows decide nothing", () => {
    // What the pane may BE is still paneSideList's answer: a disabled row must
    // not become a mode the pane can land on.
    const outside = { claude: entry("claude"), git: null };
    expect(paneSideMenu(outside).length).toBe(2);
    expect(paneSideList(outside)).toEqual(["claude"]);
    // A denied request lands on the first mode ON OFFER — which is a companion now,
    // not the fallback. This asserted `"preview"` while `preview` led every list.
    expect(activePaneSide(paneSideList(outside), "git")).toBe("claude");
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

  test("an unavailable request lands on the first mode ON OFFER", () => {
    // It used to land on Preview, because Preview led every list. Since D285 the
    // fallback is reached only when there is nothing else — so a denied `git` in a
    // folder that offers the chat lands on Claude, and only a folder offering neither
    // lands on `preview`.
    expect(activePaneSide(paneSideList({ claude: entry("claude"), git: null }), "git")).toBe(
      "claude"
    );
    expect(activePaneSide(paneSideList(NONE), "git")).toBe("preview");
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
    // Still three, and the `self` one is still needed: since D284 it identifies the
    // neither-companion fallback (a mount-backed folder) rather than the ordinary
    // no-selection state, but it is the key the hint renders under either way.
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

