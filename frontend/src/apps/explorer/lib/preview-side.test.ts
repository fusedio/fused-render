import { describe, expect, it } from "bun:test";
import type { TemplateEntry } from "@platform/lib/api";
import {
  sideSplit,
  parseSide,
  resolveSide,
  sideParam,
  sideToggleTarget,
  reconcileSideSearch,
  SIDE_OFF,
  type SideEntry,
  type SideSplitInput,
} from "@apps/explorer/lib/preview-side";
import { PANE_SIDE_OFF } from "@apps/explorer/listing/pane-side";

// `_side` as read at mount, in one call — the composition Preview makes (the
// request in state, resolved against the split on every render).
const openedAt = (search: string, split: Parameters<typeof resolveSide>[1]) =>
  resolveSide(parseSide(search), split);

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
// The chat AS THE REGISTRY ACTUALLY SHIPS IT: `templates/claude/condition.py`
// exists (it refuses mount-backed paths), so every file's `claude` entry is
// flagged conditional and is PENDING until /api/fs/conditions answers. That makes
// this the common case, not an exotic one — which is why the default-companion
// rule has to be right about it.
const gatedClaude: TemplateEntry = { ...t("claude"), conditional: true };
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
    // ...and nothing for an absent `_side` to open, which is the same rule read
    // from the other end (see resolveSide).
    expect(s.defaultSide).toBe(null);
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
    // The default a bare URL opens at is over the SETTLED list, so the pending
    // placeholder cannot be it.
    expect(s.defaultSide).toBe("claude");
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
    // ...not a toggle target, and not a `_side` the URL may hold — and, since
    // absence now means OPEN, not something a bare URL can open either.
    expect(sideToggleTarget(s.settled, null, null)).toBe(null);
    expect(openedAt("?_side=git", s)).toBe(null);
    expect(openedAt("?_side=claude", s)).toBe(null);
    expect(openedAt("", s)).toBe(null);
    expect(s.defaultSide).toBe(null);
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

// ONE VOCABULARY ACROSS BOTH SURFACES: shut is the word `off`, on a file exactly
// as on a folder. The two constants are asserted equal rather than one importing
// the other, so neither module can be respelled on its own.
describe("the closed spelling", () => {
  it("is the same word the folder pane uses", () => {
    expect(SIDE_OFF).toBe("off");
    expect(SIDE_OFF).toBe(PANE_SIDE_OFF);
  });
});

// THE REQUEST, straight off the URL — no lists consulted, so an unknown mode
// survives this far and is dropped by `resolveSide` (which is the only thing
// that knows what this file offers).
describe("parseSide", () => {
  it("reads an ABSENT `_side` as OPEN with no choice made", () => {
    // The change: absence used to mean CLOSED. It now means what it means on a
    // folder — open, at whatever this file offers first.
    expect(parseSide("")).toEqual({ open: true, mode: null });
    expect(parseSide("?zoom=2")).toEqual({ open: true, mode: null });
    // An empty value is no value.
    expect(parseSide("?_side=")).toEqual({ open: true, mode: null });
  });

  it("reads `_side=off` as SHUT", () => {
    expect(parseSide("?_side=off")).toEqual({ open: false, mode: null });
  });

  it("carries a named companion through, known or not", () => {
    expect(parseSide("?_side=git")).toEqual({ open: true, mode: "git" });
    expect(parseSide("?_side=nope")).toEqual({ open: true, mode: "nope" });
  });

  it("migrates a legacy `?_mode=claude` into the sidebar", () => {
    expect(parseSide("?_mode=claude")).toEqual({ open: true, mode: "claude" });
    // A CONTENT mode is not a sidebar request.
    expect(parseSide("?_mode=image")).toEqual({ open: true, mode: null });
  });

  it("lets an explicit shut beat the legacy param", () => {
    expect(parseSide("?_side=off&_mode=claude")).toEqual({ open: false, mode: null });
  });
});

// THE FILE'S OWN GATE, and the second flash `defaultSide` has to dodge. An own
// conditional companion counts as SETTLED for the split's existence — that
// asymmetry with the borrowed entry is deliberate and argued in the module header —
// but it must not be what an absent `_side` OPENS, because until
// /api/fs/conditions answers there is nothing to put in the column.
describe("defaultSide and a pending own gate", () => {
  const gated = (own: TemplateEntry[], git: "pending" | "yes" | "no") => ({
    ...file(own, git),
    conditionsPending: true,
  });

  it("does not open a gated companion whose verdict is still out", () => {
    const s = sideSplit(gated([gatedClaude], "no"));
    // Still settled — the split is on and the toggle has a target...
    expect(s.on).toBe(true);
    expect(names(s.settled)).toEqual(["claude"]);
    expect(sideToggleTarget(s.settled, null, null)).toBe("claude");
    // ...but a bare URL opens NOTHING until the gate answers. Opening here is an
    // empty 30% column (`src` is null, so the caller draws its spinner) that
    // vanishes if the verdict is false — on a mount, seconds later, and the
    // content pane's width jumps twice for a sidebar the file never gets.
    expect(s.defaultSide).toBe(null);
    expect(openedAt("", s)).toBe(null);
  });

  it("opens the gated companion the moment its verdict lands", () => {
    const s = sideSplit({ ...gated([gatedClaude], "no"), conditionsPending: false });
    expect(s.defaultSide).toBe("claude");
    expect(openedAt("", s)).toBe("claude");
  });

  it("prefers an UNGATED companion over a pending gated one", () => {
    // The content pane's rule, applied to this half: a gated template is never
    // the default while a normal one exists (CT-12, PT-9 — and the server's own
    // `_mark_conditions` docstring says the same).
    const s = sideSplit(gated([gatedClaude, notes], "no"));
    expect(s.defaultSide).toBe("notes");
    expect(openedAt("", s)).toBe("notes");
  });

  it("still HONOURS a deep link to a pending gated companion", () => {
    // Same posture as the pending borrowed entry: listed, deep-linkable, and the
    // verdict is what settles it. Only the DEFAULT is withheld.
    const s = sideSplit(gated([gatedClaude], "no"));
    expect(openedAt("?_side=claude", s)).toBe("claude");
  });

  it("leaves the URL alone while the only candidate is a pending gate", () => {
    const s = sideSplit(gated([gatedClaude], "no"));
    expect(
      reconcileSideSearch("", {
        splitCapable: true,
        offered: s.offered,
        open: true,
        activeSide: null,
        defaultSide: s.defaultSide,
      })
    ).toBe(null);
  });

  it("treats a settled borrowed git as the default while the own gate is out", () => {
    // `conditionsPending` is THIS FILE's verdicts; the borrowed entry has its own
    // flag and is not affected by it (Preview's `isSidePending` splits them the
    // same way).
    const s = sideSplit(gated([gatedClaude], "yes"));
    expect(s.defaultSide).toBe("git");
  });
});

describe("resolveSide", () => {
  it("OPENS a bare URL at the file's default companion", () => {
    expect(openedAt("", sideSplit(file([claude], "yes")))).toBe("claude");
    expect(openedAt("", sideSplit(file([], "yes")))).toBe("git");
    // An unknown or unhonourable request lands there too — same silent fallback
    // an unknown `_mode` gets.
    expect(openedAt("?_side=nope", sideSplit(file([claude], "yes")))).toBe("claude");
  });

  it("does NOT open on a pending placeholder alone", () => {
    // The flash this guard exists for: the only candidate is a borrowed `git`
    // whose probe may yet say "no repository here", so a bare URL opens nothing
    // and the verdict is what opens it.
    expect(openedAt("", sideSplit(file([], "pending")))).toBe(null);
    // ...and it never outranks a companion this file really has.
    expect(openedAt("", sideSplit(file([notes], "pending")))).toBe("notes");
  });

  it("honours `_side=off` however much is on offer", () => {
    expect(openedAt("?_side=off", sideSplit(file([claude], "yes")))).toBe(null);
  });

  it("keeps a ?_side=git deep link alive while the probe is pending", () => {
    // The reason the placeholder is listed at all: a list without git would
    // resolve the param to nothing and the reconcile would strip it before the
    // answer landed.
    expect(openedAt("?_side=git", sideSplit(file([], "pending")))).toBe("git");
  });

  it("falls back to the default once the probe has denied", () => {
    // It used to CLOSE here. A denial is not a request to shut the sidebar — it
    // only says this companion is not the one.
    expect(openedAt("?_side=git", sideSplit(file([claude], "no")))).toBe("claude");
    // Unless there is nothing left at all.
    expect(openedAt("?_side=git", sideSplit(file([], "no")))).toBe(null);
  });

  it("honours a settled companion", () => {
    expect(openedAt("?_side=claude", sideSplit(file([claude], "pending")))).toBe("claude");
  });

  it("is null wherever the split is not on offer", () => {
    const noSplit = sideSplit({ ...file([claude], "yes"), splitCapable: false });
    expect(openedAt("?_side=claude", noSplit)).toBe(null);
    expect(openedAt("", noSplit)).toBe(null);
    // Nothing to sit beside: a companion-only file has no content pane.
    expect(openedAt("", sideSplit({ ...file([claude], "yes"), content: [] }))).toBe(null);
  });
});

// The SPELLING a writer puts in the URL, and the one rule behind it: the default
// gets the clean URL (PT-9's rule, the folder pane's `selectSide` normalisation),
// shut says so out loud, and only a deliberate second choice is written down.
describe("sideParam", () => {
  it("deletes the param for the default companion", () => {
    expect(sideParam("claude", "claude")).toBe(null);
  });

  it("writes a non-default choice", () => {
    expect(sideParam("git", "claude")).toBe("git");
    // Nothing settled yet, so nothing is the default and a named mode is named.
    expect(sideParam("git", null)).toBe("git");
  });

  it("writes `off` for a shut sidebar", () => {
    expect(sideParam(null, "claude")).toBe(SIDE_OFF);
    expect(sideParam(null, null)).toBe(SIDE_OFF);
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
  // The shape Preview passes: the split's own verdicts plus the REQUEST's open
  // bit, since "shut" and "nothing settled yet" both read as `activeSide: null`
  // and want opposite things done to the URL.
  const o = (x: {
    offered?: boolean;
    open?: boolean;
    activeSide?: string | null;
    defaultSide?: string | null;
    splitCapable?: boolean;
  }) => ({
    splitCapable: x.splitCapable ?? true,
    offered: x.offered ?? true,
    open: x.open ?? true,
    activeSide: x.activeSide ?? null,
    defaultSide: x.defaultSide ?? null,
  });

  // A denied borrowed git takes the split off with it, and the old guard
  // ("return unless the split is on") meant the URL kept `_side=git`.
  it("clears a `_side` the probe has just denied", () => {
    expect(reconcileSideSearch("?_side=git", o({ offered: false }))).toBe("");
  });

  it("leaves a pending `_side=git` alone until the verdict", () => {
    expect(reconcileSideSearch("?_side=git", o({ activeSide: "git" }))).toBe(null);
  });

  // THE PLAIN OPEN, which is now the common case: absence means open at the
  // default, so the URL already agrees and must not grow a param. Writing
  // `_side=claude` on every file open is what made the sidecar remember it.
  it("grows no param for a sidebar open at its default", () => {
    expect(
      reconcileSideSearch("", o({ activeSide: "claude", defaultSide: "claude" }))
    ).toBe(null);
    expect(
      reconcileSideSearch("?zoom=2", o({ activeSide: "claude", defaultSide: "claude" }))
    ).toBe(null);
    // ...and clears one that says what absence already says.
    expect(
      reconcileSideSearch("?_side=claude", o({ activeSide: "claude", defaultSide: "claude" }))
    ).toBe("");
  });

  it("keeps a NON-default companion named", () => {
    expect(
      reconcileSideSearch("?_side=git", o({ activeSide: "git", defaultSide: "claude" }))
    ).toBe(null);
    expect(
      reconcileSideSearch("?zoom=2", o({ activeSide: "git", defaultSide: "claude" }))
    ).toBe("zoom=2&_side=git");
  });

  it("says `off` out loud for a shut sidebar", () => {
    expect(reconcileSideSearch("?_side=off", o({ open: false, defaultSide: "claude" }))).toBe(
      null
    );
    expect(reconcileSideSearch("", o({ open: false, defaultSide: "claude" }))).toBe("_side=off");
  });

  it("leaves `_side` ALONE while nothing is settled yet", () => {
    // Open request, no default and no active side: the only candidate is a
    // pending placeholder. Writing `_side=off` here would shut the sidebar for
    // good before the probe ever answered.
    expect(reconcileSideSearch("", o({}))).toBe(null);
    expect(reconcileSideSearch("?_side=git", o({}))).toBe(null);
  });

  it("resolves a `_side` carried in from another view", () => {
    // The folder pane writes `_side` too (listing/pane-side), and router's
    // navigate carries params across a hop. `off` is now a value this surface
    // HOLDS rather than one it strips — one vocabulary, both surfaces — but a
    // mode only the pane has is still not a state this URL may hold.
    expect(
      reconcileSideSearch("?_side=preview&sort=size", o({ activeSide: "claude", defaultSide: "claude" }))
    ).toBe("sort=size");
    expect(reconcileSideSearch("?_side=off", o({ open: false, defaultSide: "claude" }))).toBe(null);
  });

  it("never touches the URL on a surface that does not split", () => {
    // A folder's `_side` is the listing pane's, and a panel pane's `_mode` is
    // the pane bar's — a stray write here would fight both.
    for (const search of ["?_side=off", "?_side=git", "?_mode=claude"]) {
      expect(
        reconcileSideSearch(search, o({ splitCapable: false, offered: false }))
      ).toBe(null);
    }
  });

  it("migrates a legacy `_mode=claude`, once", () => {
    // The sidebar opens at claude either way now, so the migration is the
    // DELETION of `_mode` and nothing more — absence carries the rest.
    expect(
      reconcileSideSearch("?_mode=claude", o({ activeSide: "claude", defaultSide: "claude" }))
    ).toBe("");
    // ...and names it where claude is not what a bare URL would open.
    expect(
      reconcileSideSearch("?_mode=claude", o({ activeSide: "claude", defaultSide: "git" }))
    ).toBe("_side=claude");
    // Already migrated: nothing more to say.
    expect(reconcileSideSearch("", o({ activeSide: "claude", defaultSide: "claude" }))).toBe(null);
  });

  it("leaves `_mode=claude` alone where the split never took it", () => {
    // A file whose only mode is the chat renders it full width as a content
    // mode; deleting its `_mode` would be deleting a live request.
    expect(reconcileSideSearch("?_mode=claude", o({ offered: false }))).toBe(null);
  });

  it("keeps the rest of the query", () => {
    expect(
      reconcileSideSearch("?_mode=claude&zoom=2", o({ activeSide: "git", defaultSide: "claude" }))
    ).toBe("zoom=2&_side=git");
  });
});

// The lifecycle the finding is about, in the order the frames actually happen.
describe("a companion-less file in a folder with no working tree", () => {
  const pending = sideSplit(file([], "pending"));
  const denied = sideSplit(file([], "no"));

  it("opens bare: no split, no toggle, no URL churn", () => {
    expect(pending.on).toBe(false);
    // Absence means OPEN now, and this is the file it must not open for: the
    // sidebar stays down and the URL is untouched in BOTH frames, so nothing
    // flashes and nothing is written down.
    expect(openedAt("", pending)).toBe(null);
    expect(sideToggleTarget(pending.on ? pending.settled : [], null, null)).toBe(null);
    expect(
      reconcileSideSearch("", {
        splitCapable: true,
        offered: pending.offered,
        open: true,
        activeSide: null,
        defaultSide: pending.defaultSide,
      })
    ).toBe(null);
    // ...and the verdict changes none of that.
    expect(denied.on).toBe(false);
    expect(openedAt("", denied)).toBe(null);
    expect(sideToggleTarget(denied.on ? denied.settled : [], null, null)).toBe(null);
    expect(
      reconcileSideSearch("", {
        splitCapable: true,
        offered: denied.offered,
        open: true,
        activeSide: null,
        defaultSide: denied.defaultSide,
      })
    ).toBe(null);
  });

  it("opens on ?_side=git: tolerated while pending, cleared on the denial", () => {
    const want = openedAt("?_side=git", pending);
    expect(want).toBe("git");
    // Listed while pending, so the param stands.
    expect(pending.all.some((e) => e.mode === want)).toBe(true);
    expect(
      reconcileSideSearch("?_side=git", {
        splitCapable: true,
        offered: pending.offered,
        open: true,
        activeSide: "git",
        defaultSide: pending.defaultSide,
      })
    ).toBe(null);
    // The verdict lands: the entry is gone, so the active side is gone...
    expect(denied.all.some((e) => e.mode === "git")).toBe(false);
    expect(openedAt("?_side=git", denied)).toBe(null);
    // ...while the ROW stays, now saying why, which is the one thing that
    // changed: the user sees an explanation where the switcher used to shrink.
    expect(denied.menu.find((e) => e.mode === "git")?.disabledReason).toBe(NO_REPO);
    // ...and the param goes rather than sitting in a URL nothing can honour.
    expect(
      reconcileSideSearch("?_side=git", {
        splitCapable: true,
        offered: denied.offered,
        open: true,
        activeSide: null,
        defaultSide: denied.defaultSide,
      })
    ).toBe("");
  });
});
