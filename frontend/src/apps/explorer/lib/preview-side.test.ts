import { describe, expect, it } from "bun:test";
import type { TemplateEntry } from "@platform/lib/api";
import {
  sideSplit,
  initialSide,
  sideToggleTarget,
  reconcileSideSearch,
  type SideEntry,
  type SideSplitInput,
} from "@apps/explorer/lib/preview-side";

// The canned reasons, spelled out here rather than imported: they are COPY the
// user reads, so a test that re-derives them from the same constant would agree
// with any rewording, silently, including a bad one.
const NO_REPO = "Not inside a git repository";
const NO_CLAUDE = "Claude is not available for this file";

// A registered template, icon and all — the icon matters here because a disabled
// switcher row has to be able to find it.
const t = (mode: string): TemplateEntry => ({
  mode,
  path: `/tpl/${mode}/template.html`,
  icon: `/tpl/${mode}/icon.svg`,
});
const iconOf = (mode: string) => `/tpl/${mode}/icon.svg`;

// What lib/dir-mode hands over while the parent's stat + gate are still in
// flight: the mode name and nothing else.
const GIT_PLACEHOLDER: TemplateEntry = { mode: "git", path: null, icon: null };
const GIT: TemplateEntry = t("git");

const image = t("image");
const claude = t("claude");
// A companion a USER REGISTRY bound into this half: it is in the file's own
// list, but nothing here ranks or explains it. It stands in wherever a test
// needs a second own-companion beside the chat.
const notes = t("notes");

const names = (entries: TemplateEntry[]) => entries.map((e) => e.mode);
// A switcher row as the header describes it: the mode, and the reason it is
// disabled if it is one of the placeholders.
const rows = (entries: SideEntry[]) => entries.map((e) => [e.mode, e.disabledReason ?? null]);

// The three states of the borrowed entry, over a file that has some content
// template (an image, a table, its source) and whatever companions of its own.
const file = (own: TemplateEntry[], git: "pending" | "yes" | "no"): SideSplitInput => ({
  splitCapable: true,
  content: [image],
  own,
  borrowed: git === "no" ? null : git === "yes" ? GIT : GIT_PLACEHOLDER,
  borrowedPending: git === "pending",
});

describe("sideSplit", () => {
  // THE BUG: a companion-less file (a .pdf, a video, anything with no chat
  // binding) borrowed a PENDING git placeholder and the split came on
  // for it — which put a Git toggle in the bar for the length of the probe and
  // took it away again when the parent turned out not to be a repository.
  it("does not turn the split on for a pending borrowed git alone", () => {
    const s = sideSplit(file([], "pending"));
    expect(s.on).toBe(false);
    expect(names(s.settled)).toEqual([]);
    // Still LISTED, which is the other half of the policy: `_side=git` has to
    // survive until the verdict (see initialSide).
    expect(names(s.all)).toEqual(["git"]);
    expect(s.offered).toBe(true);
  });

  it("turns it on once the probe says the parent has a working tree", () => {
    const s = sideSplit(file([], "yes"));
    expect(s.on).toBe(true);
    expect(names(s.settled)).toEqual(["git"]);
    expect(names(s.all)).toEqual(["git"]);
  });

  it("is off entirely once the probe denies", () => {
    const s = sideSplit(file([], "no"));
    expect(s.on).toBe(false);
    expect(s.offered).toBe(false);
    expect(names(s.all)).toEqual([]);
  });

  // The gate-denied `git` of a file whose parent IS a repository but whose gate
  // refused it (a mount) is the same input as an absent one: dir-mode resolves an
  // explicit denial to a null entry, and from the user's side "denied" and "never
  // bound" are one fact — see mode-visibility's canned reasons.
  it("does not confuse a denied companion with a settled one", () => {
    const s = sideSplit(file([claude], "no"));
    expect(names(s.settled)).toEqual(["claude"]);
    expect(names(s.all)).toEqual(["claude"]);
  });

  // A file with a companion of its OWN splits from the first paint, verdict or
  // not — the pending placeholder rides along in `all` without deciding anything.
  it("splits on the file's own companions while the borrowed one is pending", () => {
    const s = sideSplit(file([claude, notes], "pending"));
    expect(s.on).toBe(true);
    expect(names(s.settled)).toEqual(["claude", "notes"]);
    expect(names(s.all)).toEqual(["claude", "git", "notes"]);
  });

  it("puts the settled borrowed entry in SIDEBAR_MODES order, not the input's", () => {
    const s = sideSplit(file([notes, claude], "yes"));
    expect(names(s.all)).toEqual(["claude", "git", "notes"]);
    expect(names(s.settled)).toEqual(["claude", "git", "notes"]);
  });

  // Both sides or neither: a file whose only visible mode is `claude` has no
  // content pane to sit a sidebar beside, so it renders as a full-width content
  // mode exactly as it did before the split existed.
  it("needs a content pane as well as a companion", () => {
    const s = sideSplit({ ...file([claude], "yes"), content: [] });
    expect(s.on).toBe(false);
    expect(s.offered).toBe(false);
  });

  it("is off on every surface that does not split", () => {
    const s = sideSplit({ ...file([claude], "yes"), splitCapable: false });
    expect(s.on).toBe(false);
    expect(s.offered).toBe(false);
  });
});

// THE SWITCHER'S ROWS: always the whole closed list, the unavailable ones
// explaining themselves.
describe("sideSplit's menu", () => {
  it("lists every companion over a file that has none of them", () => {
    // A .pdf outside a repository: nothing is on offer, and the menu says so
    // for each companion instead of collapsing to a control that isn't there.
    expect(rows(sideSplit(file([], "no")).menu)).toEqual([
      ["claude", NO_CLAUDE],
      ["git", NO_REPO],
    ]);
  });

  it("explains only the ones that are missing", () => {
    expect(rows(sideSplit(file([], "yes")).menu)).toEqual([
      ["claude", NO_CLAUDE],
      ["git", null],
    ]);
  });

  // A denied borrowed git is a listed, disabled Git row — the finding this
  // whole list exists for: the file used to show "Claude" alone, and a menu of
  // one hid itself, so there was no switcher at all.
  it("keeps a denied borrowed git on the list, disabled", () => {
    const menu = sideSplit(file([claude], "no")).menu;
    expect(rows(menu)).toEqual([
      ["claude", null],
      ["git", NO_REPO],
    ]);
    // A placeholder is a row, not a template: nothing to frame.
    expect(menu.find((e) => e.mode === "git")!.path).toBe(null);
  });

  // Undecided is not the same as unavailable, and must not be reported as it:
  // the entry stays REAL (the caller draws CT-12's spinner) until the verdict.
  it("leaves a pending borrowed git unexplained", () => {
    const menu = sideSplit(file([claude], "pending")).menu;
    expect(rows(menu)).toEqual([
      ["claude", null],
      ["git", null],
    ]);
    expect(menu.find((e) => e.mode === "git")).toBe(GIT_PLACEHOLDER);
  });

  // The rows are for DRAWING. Every decision keeps reading the short lists, which
  // is the whole reason `menu` is a third list rather than a flag on `all`.
  it("decides nothing", () => {
    const s = sideSplit(file([], "no"));
    expect(s.menu.length).toBe(2);
    expect(s.on).toBe(false);
    expect(s.offered).toBe(false);
    expect(names(s.settled)).toEqual([]);
    expect(names(s.all)).toEqual([]);
    // ...not a toggle target, and not a `_side` the URL may hold.
    expect(sideToggleTarget(s.settled, null, null)).toBe(null);
    expect(initialSide("?_side=git", s)).toBe(null);
    expect(initialSide("?_side=claude", s)).toBe(null);
  });

  // A DISABLED ROW IS THE MODE WITH THE CLICK TAKEN AWAY, so it keeps the mode's
  // own glyph. It shipped without this and the screenshot said it plainly: the
  // sidebar drew a boxed "C" beside a boxed "G", which reads as unknown modes
  // rather than as unavailable familiar ones.
  it("dresses a disabled row in the mode's real icon", () => {
    // The file BINDS claude — the gate is what denied it, and the
    // filter that dropped it took the icon with it (Preview re-supplies them
    // from the raw stat).
    const s = sideSplit({ ...file([], "no"), bound: [claude, GIT] });
    expect(s.menu.map((e) => [e.mode, e.icon])).toEqual([
      ["claude", iconOf("claude")],
      ["git", iconOf("git")],
    ]);
    // ...and they are still disabled rows, not entries: no path, and the reason
    // is what makes them unselectable.
    expect(rows(s.menu)).toEqual([
      ["claude", NO_CLAUDE],
      ["git", NO_REPO],
    ]);
    expect(s.menu.every((e) => e.path === null)).toBe(true);
  });

  it("takes a denied borrowed git's icon from the parent's binding", () => {
    // dir-mode keeps the parent's entry through the denial for exactly this: the
    // folder is not a repository, so there is nothing to FRAME, but the mode is
    // bound one level up and its glyph exists.
    const s = sideSplit({ ...file([claude], "no"), bound: [claude, GIT] });
    expect(s.menu.find((e) => e.mode === "git")!.icon).toBe(iconOf("git"));
    // The real entries are untouched — same objects, not rebuilt copies.
    expect(s.menu.find((e) => e.mode === "claude")).toBe(claude);
  });

  it("falls back to no icon only where the mode is bound nowhere", () => {
    // Nothing registers `claude` for this file type anywhere, so there is no
    // real glyph in existence; the caller draws its last-resort letter box.
    const s = sideSplit({ ...file([], "yes"), bound: [GIT] });
    expect(s.menu.find((e) => e.mode === "claude")!.icon).toBe(null);
  });

  // A user registry may bind a companion of its own into this half. It has no
  // canned reason and no rank, so it lands after the ranked ones rather than
  // being dropped from the rows the way an unknown mode is dropped from the order.
  it("keeps an unknown companion at the end", () => {
    const s = sideSplit({ ...file([claude], "yes"), own: [claude, notes] });
    expect(rows(s.menu)).toEqual([
      ["claude", null],
      ["git", null],
      ["notes", null],
    ]);
  });
});

describe("initialSide", () => {
  it("keeps a ?_side=git deep link alive while the probe is pending", () => {
    // The reason the placeholder is listed at all: this runs at MOUNT, and a
    // list without git would resolve the param to nothing and the reconcile
    // would strip it before the answer landed.
    expect(initialSide("?_side=git", sideSplit(file([], "pending")))).toBe("git");
  });

  it("drops it once the probe has denied", () => {
    expect(initialSide("?_side=git", sideSplit(file([], "no")))).toBe(null);
  });

  it("honours a settled companion", () => {
    expect(initialSide("?_side=claude", sideSplit(file([claude], "pending")))).toBe("claude");
  });

  it("migrates a legacy ?_mode=claude into the sidebar", () => {
    expect(initialSide("?_mode=claude", sideSplit(file([claude], "pending")))).toBe("claude");
  });

  it("ignores an unknown or absent request", () => {
    const s = sideSplit(file([claude], "yes"));
    expect(initialSide("?_side=nope", s)).toBe(null);
    expect(initialSide("", s)).toBe(null);
    // `_mode` naming a CONTENT mode is not a sidebar request.
    expect(initialSide("?_mode=image", s)).toBe(null);
  });

  it("is null wherever the split is not on offer", () => {
    expect(initialSide("?_side=claude", sideSplit({ ...file([claude], "yes"), splitCapable: false }))).toBe(null);
  });
});

describe("sideToggleTarget", () => {
  const targets = (i: SideSplitInput) => {
    const s = sideSplit(i);
    return s.on ? s.settled : [];
  };

  // THE BUG, second half: the toggle button renders from this, so a pending
  // placeholder here is a button that appears and vanishes on every open.
  it("has no target while the only candidate is a pending borrowed git", () => {
    expect(sideToggleTarget(targets(file([], "pending")), null, null)).toBe(null);
  });

  it("never outranks a settled companion with a pending one", () => {
    // defaultSidebarMode ranks Git above an unranked companion, so an unfiltered
    // list would have opened the pending Git over this file's real one.
    expect(sideToggleTarget(targets(file([notes], "pending")), null, null)).toBe("notes");
    // ...and picks Git once it is real.
    expect(sideToggleTarget(targets(file([notes], "yes")), null, null)).toBe("git");
  });

  it("prefers the chat when the file has one", () => {
    expect(sideToggleTarget(targets(file([claude, notes], "yes")), null, null)).toBe("claude");
  });

  it("reopens the last companion the user had open", () => {
    expect(sideToggleTarget(targets(file([claude, notes], "yes")), null, "notes")).toBe("notes");
    // ...unless it is no longer on offer here.
    expect(sideToggleTarget(targets(file([claude], "no")), null, "git")).toBe("claude");
    // ...including when it is only PENDING: the button must not offer to open a
    // column that may have nothing to show.
    expect(sideToggleTarget(targets(file([claude], "pending")), null, "git")).toBe("claude");
  });

  it("acts on whatever is open, first of all", () => {
    expect(sideToggleTarget(targets(file([claude], "yes")), "git", "claude")).toBe("git");
  });
});

describe("reconcileSideSearch", () => {
  // A denied borrowed git takes the split off with it, and the old guard
  // ("return unless the split is on") meant the URL kept `_side=git` — which the
  // session sidecar then recorded and replayed on the next bare open, so the
  // stale param outlived the tab.
  it("clears a `_side` the probe has just denied", () => {
    expect(
      reconcileSideSearch("?_side=git", { splitCapable: true, offered: false, activeSide: null })
    ).toBe("");
  });

  it("leaves a pending `_side=git` alone until the verdict", () => {
    expect(
      reconcileSideSearch("?_side=git", { splitCapable: true, offered: true, activeSide: "git" })
    ).toBe(null);
  });

  it("clears a `_side` carried in from another view", () => {
    // The folder pane writes `_side` too (listing/pane-side), and router's
    // navigate carries params across a hop.
    expect(
      reconcileSideSearch("?_side=off", { splitCapable: true, offered: true, activeSide: null })
    ).toBe("");
    expect(
      reconcileSideSearch("?_side=preview&sort=size", {
        splitCapable: true,
        offered: true,
        activeSide: null,
      })
    ).toBe("sort=size");
  });

  it("never touches the URL on a surface that does not split", () => {
    // A folder's `_side` is the listing pane's, and a panel pane's `_mode` is
    // the pane bar's — a stray write here would fight both.
    for (const search of ["?_side=off", "?_side=git", "?_mode=claude"]) {
      expect(
        reconcileSideSearch(search, { splitCapable: false, offered: false, activeSide: null })
      ).toBe(null);
    }
  });

  it("migrates a legacy `_mode=claude` to `_side=claude`, once", () => {
    expect(
      reconcileSideSearch("?_mode=claude", { splitCapable: true, offered: true, activeSide: "claude" })
    ).toBe("_side=claude");
    // Already migrated: nothing more to say.
    expect(
      reconcileSideSearch("?_side=claude", { splitCapable: true, offered: true, activeSide: "claude" })
    ).toBe(null);
  });

  it("leaves `_mode=claude` alone where the split never took it", () => {
    // A file whose only mode is the chat renders it full width as a content
    // mode; deleting its `_mode` would be deleting a live request.
    expect(
      reconcileSideSearch("?_mode=claude", { splitCapable: true, offered: false, activeSide: null })
    ).toBe(null);
  });

  it("keeps the rest of the query", () => {
    expect(
      reconcileSideSearch("?_mode=claude&zoom=2", {
        splitCapable: true,
        offered: true,
        activeSide: "claude",
      })
    ).toBe("zoom=2&_side=claude");
    expect(
      reconcileSideSearch("?zoom=2", { splitCapable: true, offered: true, activeSide: "git" })
    ).toBe("zoom=2&_side=git");
  });
});

// The lifecycle the finding is about, in the order the frames actually happen.
describe("a companion-less file in a folder with no working tree", () => {
  const pending = sideSplit(file([], "pending"));
  const denied = sideSplit(file([], "no"));

  it("opens bare: no split, no toggle, no URL churn", () => {
    expect(pending.on).toBe(false);
    expect(sideToggleTarget(pending.on ? pending.settled : [], null, null)).toBe(null);
    expect(
      reconcileSideSearch("", { splitCapable: true, offered: pending.offered, activeSide: null })
    ).toBe(null);
    // ...and the verdict changes none of that, so nothing flashed.
    expect(denied.on).toBe(false);
    expect(sideToggleTarget(denied.on ? denied.settled : [], null, null)).toBe(null);
    expect(
      reconcileSideSearch("", { splitCapable: true, offered: denied.offered, activeSide: null })
    ).toBe(null);
  });

  it("opens on ?_side=git: tolerated while pending, cleared on the denial", () => {
    const want = initialSide("?_side=git", pending);
    expect(want).toBe("git");
    // Listed while pending, so the param stands.
    expect(pending.all.some((e) => e.mode === want)).toBe(true);
    expect(
      reconcileSideSearch("?_side=git", {
        splitCapable: true,
        offered: pending.offered,
        activeSide: "git",
      })
    ).toBe(null);
    // The verdict lands: the entry is gone, so the active side is gone...
    expect(denied.all.some((e) => e.mode === "git")).toBe(false);
    // ...while the ROW stays, now saying why, which is the one thing that
    // changed: the user sees an explanation where the switcher used to shrink.
    expect(denied.menu.find((e) => e.mode === "git")?.disabledReason).toBe(NO_REPO);
    // ...and the param goes with it rather than into the session sidecar.
    expect(
      reconcileSideSearch("?_side=git", {
        splitCapable: true,
        offered: denied.offered,
        activeSide: null,
      })
    ).toBe("");
  });
});
