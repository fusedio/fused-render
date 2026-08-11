import { describe, expect, it } from "bun:test";
import type { TemplateEntry } from "@platform/lib/api";
import {
  sideSplit,
  initialSide,
  sideToggleTarget,
  reconcileSideSearch,
  type SideSplitInput,
} from "@apps/explorer/lib/preview-side";

const t = (mode: string): TemplateEntry => ({
  mode,
  path: `/tpl/${mode}/template.html`,
  icon: null,
});

// What lib/dir-mode hands over while the parent's stat + gate are still in
// flight: the mode name and nothing else.
const GIT_PLACEHOLDER: TemplateEntry = { mode: "git", path: null, icon: null };
const GIT: TemplateEntry = t("git");

const image = t("image");
const claude = t("claude");
const history = t("history");

const names = (entries: TemplateEntry[]) => entries.map((e) => e.mode);

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
  // THE BUG: a companion-less file (a .pdf, a video, anything with no chat and
  // no history binding) borrowed a PENDING git placeholder and the split came on
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

  // A file with a companion of its OWN splits from the first paint, verdict or
  // not — the pending placeholder rides along in `all` without deciding anything.
  it("splits on the file's own companions while the borrowed one is pending", () => {
    const s = sideSplit(file([claude, history], "pending"));
    expect(s.on).toBe(true);
    expect(names(s.settled)).toEqual(["claude", "history"]);
    expect(names(s.all)).toEqual(["claude", "git", "history"]);
  });

  it("puts the settled borrowed entry in SIDEBAR_MODES order, not the input's", () => {
    const s = sideSplit(file([history, claude], "yes"));
    expect(names(s.all)).toEqual(["claude", "git", "history"]);
    expect(names(s.settled)).toEqual(["claude", "git", "history"]);
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
    expect(initialSide("?_side=history", sideSplit(file([history], "pending")))).toBe("history");
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
    // defaultSidebarMode's order is Claude / Git / History, so an unfiltered
    // list would have opened the pending Git over this file's real History.
    expect(sideToggleTarget(targets(file([history], "pending")), null, null)).toBe("history");
    // ...and picks Git once it is real.
    expect(sideToggleTarget(targets(file([history], "yes")), null, null)).toBe("git");
  });

  it("prefers the chat when the file has one", () => {
    expect(sideToggleTarget(targets(file([claude, history], "yes")), null, null)).toBe("claude");
  });

  it("reopens the last companion the user had open", () => {
    expect(sideToggleTarget(targets(file([claude, history], "yes")), null, "history")).toBe("history");
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
      reconcileSideSearch("?zoom=2", { splitCapable: true, offered: true, activeSide: "history" })
    ).toBe("zoom=2&_side=history");
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
