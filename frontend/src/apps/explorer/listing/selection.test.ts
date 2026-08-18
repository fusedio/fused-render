// Pure bits of the selection model. The stateful half (the hook, the keyboard
// handler, the reconcile) needs a DOM and a React renderer, neither of which
// the frontend test setup has — see shortcut-chord.test.ts.
import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import {
  EMPTY_SELECTION,
  firstEntryPath,
  oneSelected,
  pathFromSelParam,
  cameFromSelParam,
  rangeBetween,
  rowPressAction,
  selParam,
  selectionAfterVanish,
  INITIAL_SEARCH_SELECT,
  nextSearchSelection,
  searchAutoSelectPath,
} from "./selection";
// The whole module, to assert what it no longer offers (see the folder
// auto-select block below).
import * as selectionModule from "./selection";

// Rows as the listing hands them over: the rendered (sorted) path order plus
// the path→row map the table builds anyway.
function rows(spec: Array<[string, boolean]>) {
  const paths = spec.map(([name]) => "/d/" + name);
  const byPath = new Map(spec.map(([name, isDir]) => ["/d/" + name, { isDir }]));
  return { paths, byPath };
}

describe("firstEntryPath", () => {
  test("picks the first entry in RENDERED order", () => {
    const { paths, byPath } = rows([
      ["a.txt", false],
      ["b.txt", false],
    ]);
    expect(firstEntryPath(paths, byPath)).toBe("/d/a.txt");
  });

  test("a directory the sort put first is picked, not skipped", () => {
    // The usual shape of a folder: dirs at the top, files under them. The top
    // row is what the eye lands on, so it is what previews — as a peek.
    const { paths, byPath } = rows([
      ["src", true],
      ["docs", true],
      ["README.md", false],
      ["setup.py", false],
    ]);
    expect(firstEntryPath(paths, byPath)).toBe("/d/src");
  });

  test("follows the order it is given, not the alphabet", () => {
    // A descending sort renders the same rows the other way round, and the
    // auto-selection must land on what is actually at the top of the table.
    const { paths, byPath } = rows([
      ["z.txt", false],
      ["a.txt", false],
    ]);
    expect(firstEntryPath(paths, byPath)).toBe("/d/z.txt");
  });

  test("an empty folder has nothing to preview", () => {
    expect(firstEntryPath([], new Map())).toBeNull();
  });

  test("a path with no rendered row is not selectable", () => {
    const { byPath } = rows([["a.txt", false]]);
    expect(firstEntryPath(["/d/ghost.txt"], byPath)).toBeNull();
  });
});

// OPENING A FOLDER SELECTS NOTHING (FS-16, D278 — superseding D263's answer
// and, with it, D240's). The rule is now an ABSENCE, so there is no function
// left to test and the whole `autoSelectPath` suite is gone with the decision it
// pinned: a folder's pane opens on its self target and its `Select a file to
// preview.` hint, which is what FS-11 already describes.
//
// What is left to defend is that the behaviour does not creep back, and that
// takes both halves below. A re-added helper with no caller would pass a
// "Listing does not call it" check while re-inviting the behaviour; a caller
// re-added under another name would pass an "export is gone" check.
describe("a freshly opened folder selects nothing", () => {
  test("no folder auto-select decision is exported at all", () => {
    const exported = Object.keys(selectionModule);
    expect(exported).not.toContain("autoSelectPath");
    // Its "has something already claimed the selection?" guard existed only to
    // hold that shot back, so it goes with it.
    expect(exported).not.toContain("selectionClaimed");
    // SEARCH auto-selection is a DIFFERENT surface and is untouched: typing a
    // query still lands on the top hit, so the pane and Enter have a target.
    expect(exported).toContain("searchAutoSelectPath");
    // …and its shared "first row that is actually on screen" helper stays too.
    expect(exported).toContain("firstEntryPath");
  });

  test("the listing runs no auto-select over a folder's rows", () => {
    // The effect needs a DOM and a React renderer this setup does not have (see
    // the file header), so the absence is pinned at the source. The lookbehind
    // keeps `searchAutoSelectPath` from matching.
    const src = readFileSync(join(import.meta.dir, "../Listing.tsx"), "utf8");
    expect(src).not.toMatch(/(?<![A-Za-z])autoSelectPath/);
    expect(src).not.toContain("selectionClaimed");
    expect(src).not.toContain("autoSelectedRef");
    // Deep links and the cross-remount stash are NOT part of this removal: a
    // `?sel=` and a click in the pre-stat scaffold still seed the selection.
    expect(src).toContain("nextSearchSelection(");
  });
});

// What happens when the LEAD's row is not among the rendered rows — the one
// remaining way a row the user never chose could end up selected (D279).
//
// Two situations reach this, and they get opposite answers. The test writes both
// out because the distinction IS the rule: it hangs entirely on whether the
// selection was ever seen as a live row of this listing.
describe("selectionAfterVanish", () => {
  const rows = ["/d/a.txt", "/d/b.txt", "/d/c.txt"];

  test("a selection that was never a live row selects NOTHING", () => {
    // The `?sel=` miss: a bookmark or a shared link names `old.txt`, the file is
    // gone, so the seeded lead is a path this folder does not have and never had.
    // Clamping there picks row one — a file nobody named, previewed in an iframe
    // nobody asked for, which is exactly what D278 removed.
    expect(selectionAfterVanish(rows, -1)).toEqual(EMPTY_SELECTION);
  });

  test("a row that vanished WHILE the folder was open re-anchors to its slot", () => {
    // The half that must not change: an external delete, or a rename the user
    // made themselves, should leave the selection on the row that took the old
    // one's place — not empty, and not row one.
    expect(selectionAfterVanish(rows, 1)).toEqual(oneSelected("/d/b.txt"));
  });

  test("a slot past the end lands on the last row", () => {
    // The folder shrank under the selection (a multi-delete of the tail).
    expect(selectionAfterVanish(rows, 7)).toEqual(oneSelected("/d/c.txt"));
    expect(selectionAfterVanish(rows, 2)).toEqual(oneSelected("/d/c.txt"));
  });

  test("no rows left to re-anchor to selects nothing", () => {
    // Emptied folder: there is no neighbour, however well anchored it was.
    expect(selectionAfterVanish([], 1)).toEqual(EMPTY_SELECTION);
    expect(selectionAfterVanish([], -1)).toEqual(EMPTY_SELECTION);
  });

  // The reconcile effect needs a DOM and a React renderer this setup does not
  // have, so the wiring is pinned at the source: the decision must not be
  // re-inlined as a bare clamp, which is the shape the bug had.
  test("the reconcile's vanished-lead path defers to this decision", () => {
    const src = readFileSync(join(import.meta.dir, "./useListingSelection.ts"), "utf8");
    expect(src).toContain("selectionAfterVanish(rows, lastSelIndexRef.current)");
  });
});

describe("rangeBetween", () => {
  test("inclusive, in row order, whichever end was clicked first", () => {
    const r = ["a", "b", "c", "d"];
    expect(rangeBetween(r, "b", "d")).toEqual(["b", "c", "d"]);
    expect(rangeBetween(r, "d", "b")).toEqual(["b", "c", "d"]);
  });

  test("an anchor that is gone collapses onto the target", () => {
    expect(rangeBetween(["a", "b"], "gone", "b")).toEqual(["b"]);
    expect(rangeBetween(["a", "b"], "a", "gone")).toEqual([]);
  });
});

// ONE press model for the whole explorer: a press selects, a double click
// opens. The pure half of it is which SELECTION a press means; the opening is
// the row's onDoubleClick and needs no decision at all.
//
// Answered on POINTERDOWN rather than on click, because rows are drag sources
// and a draggable element does not reliably deliver the click that follows the
// press — the failure that killed Shift/Cmd-click once and plain presses on
// part of a row later.
describe("rowPressAction", () => {
  const press = (mod: boolean, shift: boolean, inMultiSelection = false) =>
    rowPressAction({ mod, shift, inMultiSelection });

  test("a plain press selects that row alone", () => {
    expect(press(false, false)).toBe("select");
  });

  test("Shift extends the range", () => {
    expect(press(false, true)).toBe("extend");
  });

  test("Mod toggles the row in or out", () => {
    expect(press(true, false)).toBe("toggle");
  });

  test("Mod wins over Shift when both are held", () => {
    // Both modifiers down is ambiguous, and Finder resolves it the same way:
    // the toggle is the more precise gesture, and an accidental Shift held
    // while Mod-picking rows must not replace the picks with a range.
    expect(press(true, true)).toBe("toggle");
  });

  // --- the one press that cannot be answered on the press -------------------

  test("a plain press INSIDE a multi-selection defers", () => {
    // Collapsing here would make a multi-row drag impossible: every drag of a
    // selection begins with a press on one of the rows in it, so answering
    // "select" on the press would leave one row to drag, every time.
    expect(press(false, false, true)).toBe("defer");
  });

  test("a press on the ONLY selected row does not defer", () => {
    // `inMultiSelection` is about a selection of MANY. With one row selected,
    // "select" already is the answer a deferred collapse would arrive at, and
    // deferring would only make the highlight land later for no reason.
    expect(press(false, false, false)).toBe("select");
  });

  test("a MODIFIED press inside a multi-selection is never deferred", () => {
    // Shift/Mod are never the start of a plain drag of the selection, so they
    // keep their immediate meaning even on a row that is part of one — which is
    // also what keeps them independent of the click event.
    expect(press(true, false, true)).toBe("toggle");
    expect(press(false, true, true)).toBe("extend");
    expect(press(true, true, true)).toBe("toggle");
  });
});

// `?sel=` — the primary selection, in the URL, so a reload or a shared link
// comes back to the same row with the same thing in the preview pane.
describe("selParam / pathFromSelParam", () => {
  test("a row of this folder is its name", () => {
    expect(selParam("/d", "/d/notes.md")).toBe("notes.md");
    expect(pathFromSelParam("/d", "notes.md")).toBe("/d/notes.md");
  });

  test("a search hit keeps its relative path", () => {
    // Search rows are `base + "/" + entry.rel`, so the suffix is the whole
    // relative path — one rule covers both view modes.
    expect(selParam("/d", "/d/sub/deep/notes.md")).toBe("sub/deep/notes.md");
    expect(pathFromSelParam("/d", "sub/deep/notes.md")).toBe("/d/sub/deep/notes.md");
  });

  test("nothing selected is no param at all", () => {
    expect(selParam("/d", null)).toBeNull();
  });

  test("a path outside this folder is not this folder's selection", () => {
    // Can happen for a beat mid-navigation, and writing it would put another
    // folder's row on this folder's URL.
    expect(selParam("/d", "/other/notes.md")).toBeNull();
    expect(selParam("/d", "/dd/notes.md")).toBeNull();
  });

  test("the fs root round-trips", () => {
    // Listing's `base` for "/" is the empty string (its trailing slash is
    // stripped), so the join is still "" + "/" + name.
    expect(selParam("", "/etc")).toBe("etc");
    expect(pathFromSelParam("", "etc")).toBe("/etc");
  });

  test("a hostile or empty param resolves to nothing", () => {
    // The param is a URL, i.e. attacker-supplied. An absolute value or one
    // climbing out of the folder must not become a path the pane will stat.
    expect(pathFromSelParam("/d", null)).toBeNull();
    expect(pathFromSelParam("/d", "")).toBeNull();
    expect(pathFromSelParam("/d", "/etc/passwd")).toBeNull();
    expect(pathFromSelParam("/d", "../../etc/passwd")).toBeNull();
    expect(pathFromSelParam("/d", "sub/../../etc")).toBeNull();
  });

  test("a dotfile is a normal name, not a traversal", () => {
    expect(pathFromSelParam("/d", ".gitignore")).toBe("/d/.gitignore");
    expect(pathFromSelParam("/d", "..hidden")).toBe("/d/..hidden");
  });
});

describe("cameFromSelParam", () => {
  test("an ancestor hop selects the child it came through", () => {
    // The Finder rule: go up and the folder you left is highlighted.
    expect(cameFromSelParam("/a/b", "/a/b/c")).toBe("c");
    expect(cameFromSelParam("/a", "/a/b/c/d")).toBe("b");
  });

  test("the child, never the whole remainder", () => {
    // A crumb several levels up still selects its OWN child — the only row it
    // actually has — not the deep path the user came from.
    expect(cameFromSelParam("/a", "/a/b/c")).toBe("b");
  });

  test("the fs root is an ancestor like any other", () => {
    expect(cameFromSelParam("/", "/etc/hosts")).toBe("etc");
    // Listing's `base` form: the trailing slash already stripped off.
    expect(cameFromSelParam("", "/etc/hosts")).toBe("etc");
  });

  test("a windows drive root keeps its slash out of the name", () => {
    expect(cameFromSelParam("C:/", "C:/Users/me")).toBe("Users");
    expect(cameFromSelParam("C:/Users", "C:/Users/me")).toBe("me");
  });

  test("staying put selects nothing", () => {
    expect(cameFromSelParam("/a/b", "/a/b")).toBeNull();
    expect(cameFromSelParam("/a/b", "/a/b/")).toBeNull();
  });

  test("a target that is not an ancestor selects nothing", () => {
    // A typed path, a bookmark, a sibling: there is no "came from" row there.
    expect(cameFromSelParam("/x", "/a/b")).toBeNull();
    // Prefix-of-the-string is not prefix-of-the-path.
    expect(cameFromSelParam("/a/bb", "/a/bbb/c")).toBeNull();
    // Downward is not an ancestor hop either.
    expect(cameFromSelParam("/a/b/c", "/a/b")).toBeNull();
  });
});

// The unified model, pinned at the source: neither half may be conditioned on
// the preview pane again. A single click used to OPEN when the pane was off
// and merely SELECT when it was on — two click models in one view, decided by
// a layout state the user no longer even controls (listing/pane.ts).
describe("the listing rows wire both halves of the model", () => {
  const src = readFileSync(join(import.meta.dir, "../Listing.tsx"), "utf8");

  test("a press selects and never navigates", () => {
    const press = src.slice(
      src.indexOf("const onRowPointerDown ="),
      src.indexOf("const onRowPointerUp ="),
    );
    expect(press).toContain("rowPressAction");
    expect(press).not.toContain("navigate(");
  });

  test("a double click always opens, pane or no pane", () => {
    const dbl = src.slice(
      src.indexOf("const onRowDoubleClick ="),
      src.indexOf("// Kill the browser's own text selection"),
    );
    expect(dbl).toContain("navigate(row.path");
    expect(dbl).not.toContain("pane.on");
  });

  // The failure this whole model exists to rule out: rows are drag sources, and
  // a draggable element does not reliably deliver the click after the press. A
  // selection path hung off `click` is one that can silently stop working.
  //
  // Checked over BOTH row shapes — the plain listing's and the search hits' —
  // because they are written out separately and only one of them was ever
  // wrong at a time. `onDoubleClick` is deliberately untouched: dblclick fires
  // whether or not a click did.
  test("neither row shape selects on a click event", () => {
    expect(src).not.toContain("onRowClick");
    const rows = src.split("data-flip-key={childPath}").slice(1);
    expect(rows).toHaveLength(2);
    for (const row of rows) {
      const props = row.slice(0, row.indexOf("onContextMenu"));
      expect(props).toContain("onPointerDown={(e) => onRowPointerDown(e, childPath)}");
      expect(props).not.toContain("onClick=");
      expect(props).not.toContain("onMouseDown=");
    }
  });
});

describe("searchAutoSelectPath", () => {
  const sel = (lead: string) => oneSelected(lead);

  test("lands on the top hit when the results first arrive", () => {
    const { paths, byPath } = rows([
      ["hit1.ts", false],
      ["hit2.ts", false],
    ]);
    expect(searchAutoSelectPath(paths, byPath, EMPTY_SELECTION, false)).toBe("/d/hit1.ts");
  });

  test("follows the ranking as the results refine under the same query", () => {
    // Typing narrows the corpus and re-ranks it. An auto-placed selection is
    // the app's own guess, so it moves to whatever is now the best match
    // rather than pinning row one of a ranking nobody is looking at any more.
    const { paths, byPath } = rows([
      ["better.ts", false],
      ["hit1.ts", false],
    ]);
    expect(searchAutoSelectPath(paths, byPath, sel("/d/hit1.ts"), false)).toBe("/d/better.ts");
  });

  test("does not re-select the row it already sits on", () => {
    // Returning the same path anyway would be a state write per re-rank, and
    // each one of those remounts the preview pane's iframe.
    const { paths, byPath } = rows([
      ["hit1.ts", false],
      ["hit2.ts", false],
    ]);
    expect(searchAutoSelectPath(paths, byPath, sel("/d/hit1.ts"), false)).toBeNull();
  });

  test("respects a selection the USER moved, even as the ranking changes", () => {
    // The analogue of the folder rule: auto-select fills a selection nobody
    // claimed, it never overrules one somebody chose.
    const { paths, byPath } = rows([
      ["better.ts", false],
      ["chosen.ts", false],
    ]);
    expect(searchAutoSelectPath(paths, byPath, sel("/d/chosen.ts"), true)).toBeNull();
  });

  test("takes the top hit back when the user's row leaves the results", () => {
    // Nothing left to respect — the row they picked is not on screen, and
    // holding the selection there leaves the pane previewing a vanished path.
    const { paths, byPath } = rows([
      ["better.ts", false],
      ["other.ts", false],
    ]);
    expect(searchAutoSelectPath(paths, byPath, sel("/d/gone.ts"), true)).toBe("/d/better.ts");
  });

  test("selects nothing when the query matched nothing", () => {
    expect(searchAutoSelectPath([], new Map(), EMPTY_SELECTION, false)).toBeNull();
    // ...and does not clear a selection the user made either
    expect(searchAutoSelectPath([], new Map(), sel("/d/chosen.ts"), true)).toBeNull();
  });

  test("skips a path with no rendered row", () => {
    const byPath = new Map([["/d/b.ts", { isDir: false }]]);
    expect(
      searchAutoSelectPath(["/d/a.ts", "/d/b.ts"], byPath, EMPTY_SELECTION, false),
    ).toBe("/d/b.ts");
  });

  test("leaves a selection the user deliberately cleared alone", () => {
    // Same rule the folder shot follows: Escape means Escape.
    const { paths, byPath } = rows([["a.ts", false]]);
    expect(searchAutoSelectPath(paths, byPath, EMPTY_SELECTION, true)).toBeNull();
  });
});

// Whose selection it is, tracked across re-ranks and query changes. This used
// to live as a pair of refs in Listing, where it could not be tested — and it
// was wrong there: the record was reset on every query change, which
// reclassified the user's own selection as the app's guess and let auto-select
// overwrite it.
describe("nextSearchSelection", () => {
  const rowset = (...names: string[]) => rows(names.map((n) => [n, false] as [string, boolean]));

  test("fills an empty selection with the top hit and remembers it placed it", () => {
    const { paths, byPath } = rowset("hit1.ts", "hit2.ts");
    const out = nextSearchSelection(INITIAL_SEARCH_SELECT, paths, byPath, EMPTY_SELECTION, true);
    expect(out.select).toBe("/d/hit1.ts");
    expect(out.state).toEqual({ autoPlaced: "/d/hit1.ts", userClaimed: false });
  });

  test("follows the ranking while the selection is still its own guess", () => {
    const { paths, byPath } = rowset("better.ts", "hit1.ts");
    const state = { autoPlaced: "/d/hit1.ts", userClaimed: false };
    const out = nextSearchSelection(state, paths, byPath, oneSelected("/d/hit1.ts"), true);
    expect(out.select).toBe("/d/better.ts");
  });

  test("notices the user moving the selection and yields to it", () => {
    const { paths, byPath } = rowset("better.ts", "chosen.ts");
    // auto-select had placed better.ts; the lead is somewhere else now
    const state = { autoPlaced: "/d/better.ts", userClaimed: false };
    const out = nextSearchSelection(state, paths, byPath, oneSelected("/d/chosen.ts"), true);
    expect(out.select).toBeNull();
    expect(out.state.userClaimed).toBe(true);
  });

  test("a user's selection SURVIVES a query change while it is still a result", () => {
    // The live repro: search "readme.md", arrow down to
    // Downloads/collab-canvas-share/README.md, then retype the query as
    // "collab-canvas". That path is still in the results, so it must stay
    // selected — the new query re-ranks the rows, it does not revoke the
    // user's choice.
    const claimed = { autoPlaced: "/d/top.ts", userClaimed: true };
    const first = rowset("README.md", "other.ts");
    const held = nextSearchSelection(claimed, first.paths, first.byPath, oneSelected("/d/README.md"), true);
    expect(held.select).toBeNull();
    // ...now the query changes and the ranking is completely different
    const after = rowset("collab-canvas-share.zip", "a.ts", "b.ts", "README.md");
    const out = nextSearchSelection(held.state, after.paths, after.byPath, oneSelected("/d/README.md"), true);
    expect(out.select).toBeNull(); // NOT the new top hit
    expect(out.state.userClaimed).toBe(true);
  });

  test("an AUTO-placed selection does not survive a query change", () => {
    // The other half of the same rule: the app's own guess is re-made against
    // whatever the new query ranked first.
    const state = { autoPlaced: "/d/hit1.ts", userClaimed: false };
    const after = rowset("brandnew.ts", "hit1.ts");
    const out = nextSearchSelection(state, after.paths, after.byPath, oneSelected("/d/hit1.ts"), true);
    expect(out.select).toBe("/d/brandnew.ts");
    expect(out.state).toEqual({ autoPlaced: "/d/brandnew.ts", userClaimed: false });
  });

  test("the top hit is taken back when the user's row drops out", () => {
    const claimed = { autoPlaced: "/d/old.ts", userClaimed: true };
    const { paths, byPath } = rowset("survivor.ts");
    const out = nextSearchSelection(claimed, paths, byPath, oneSelected("/d/gone.ts"), true);
    expect(out.select).toBe("/d/survivor.ts");
    // ...and the claim is released, so the ranking is followed again after
    expect(out.state).toEqual({ autoPlaced: "/d/survivor.ts", userClaimed: false });
  });

  test("clearing the selection is itself a claim, and stays cleared", () => {
    const state = { autoPlaced: "/d/hit1.ts", userClaimed: false };
    const { paths, byPath } = rowset("hit1.ts", "hit2.ts");
    const out = nextSearchSelection(state, paths, byPath, EMPTY_SELECTION, true);
    expect(out.select).toBeNull();
    expect(out.state.userClaimed).toBe(true);
  });

  test("a user who moves back ONTO the auto-placed row still owns it", () => {
    // The claim has to be remembered, not re-derived from "the lead is not
    // where we put it" — arrow down and back up again and the lead is exactly
    // where we put it, while the user has very deliberately parked there. Get
    // this wrong and the selection silently resumes drifting with the ranking
    // under someone who is holding it still.
    const claimed = { autoPlaced: "/d/a.ts", userClaimed: true };
    const { paths, byPath } = rowset("new-top.ts", "a.ts");
    const out = nextSearchSelection(claimed, paths, byPath, oneSelected("/d/a.ts"), true);
    expect(out.select).toBeNull();
    expect(out.state.userClaimed).toBe(true);
  });

  test("the effect never resets its state per query", () => {
    // The regression this whole module move fixes: Listing kept the record in
    // a ref cleared by a `[q]` effect, so every keystroke told the next
    // re-rank that the user's selection was the app's own guess — and it was
    // duly overwritten with the new top hit. The state must be threaded, not
    // reset; a query change is not a reason to forget who chose the row.
    const src = readFileSync(join(import.meta.dir, "../Listing.tsx"), "utf8");
    expect(src).toContain("nextSearchSelection(");
    expect(src).not.toMatch(/searchSelectRef\.current\s*=\s*INITIAL_SEARCH_SELECT/);
    // the ref is only ever assigned the state the decision hands back
    const writes = src.match(/searchSelectRef\.current\s*=\s*[^;]+/g) ?? [];
    expect(writes).toEqual(["searchSelectRef.current = state"]);
  });

  test("an empty selection before anything was placed is still filled", () => {
    // Distinct from the case above: nothing has been auto-placed yet, so an
    // empty selection is "results just arrived", not "the user cleared it".
    const { paths, byPath } = rowset("hit1.ts");
    const out = nextSearchSelection(INITIAL_SEARCH_SELECT, paths, byPath, EMPTY_SELECTION, true);
    expect(out.select).toBe("/d/hit1.ts");
  });
});

// -- rows that do not answer the current query --------------------------------
//
// The ranked box never blanks the list: while the next answer is in flight the
// PREVIOUS query's rows stay on screen, dimmed. That is deliberate, and it
// creates a hazard the walk never had — on the walk path a query change
// emptied the rows for a commit (lib/search-hold is query-tagged), so
// auto-select had nothing to place and Enter fell out at `if (!rows.length)`.
// Rows that outlive their query must therefore be DISPLAY-ONLY: visible, but
// never the thing a default action acts on.

describe("nextSearchSelection over rows that answer an older query", () => {
  const rowset = (...names: string[]) => rows(names.map((n) => [n, false] as [string, boolean]));

  test("does not auto-place a selection on them", () => {
    // Type "read", get README.md selected; type "me" and the rows are still
    // README.md while the answer for "readme" is in flight. Selecting it again
    // arms Enter and every destructive shortcut on a row that answers nothing.
    const { paths, byPath } = rowset("README.md");
    const out = nextSearchSelection(INITIAL_SEARCH_SELECT, paths, byPath, EMPTY_SELECTION, false);
    expect(out.select).toBeNull();
    expect(out.clear).toBe(false);
  });

  test("withdraws a selection IT placed once the rows go stale", () => {
    // The dangerous half: the row was auto-selected while it was the answer,
    // and it stays selected as the query moves past it — so Cmd+Backspace
    // trashes a file the user is no longer looking for.
    const { paths, byPath } = rowset("README.md");
    const placed = nextSearchSelection(INITIAL_SEARCH_SELECT, paths, byPath, EMPTY_SELECTION, true);
    expect(placed.select).toBe("/d/README.md");
    const stale = nextSearchSelection(placed.state, paths, byPath,
                                      oneSelected("/d/README.md"), false);
    expect(stale.clear).toBe(true);
    expect(stale.select).toBeNull();
  });

  test("leaves a selection the USER made alone", () => {
    // Their choice outlives a query change — that rule is older than this one
    // and this must not quietly undo it. They pointed at a row they can see.
    const { paths, byPath } = rowset("hit1.ts", "chosen.ts");
    const claimed = { autoPlaced: "/d/hit1.ts", userClaimed: true };
    const out = nextSearchSelection(claimed, paths, byPath, oneSelected("/d/chosen.ts"), false);
    expect(out.clear).toBe(false);
    expect(out.select).toBeNull();
  });

  test("withdraws only once — a cleared selection is not re-cleared", () => {
    const { paths, byPath } = rowset("README.md");
    const placed = nextSearchSelection(INITIAL_SEARCH_SELECT, paths, byPath, EMPTY_SELECTION, true);
    const first = nextSearchSelection(placed.state, paths, byPath,
                                      oneSelected("/d/README.md"), false);
    const second = nextSearchSelection(first.state, paths, byPath, EMPTY_SELECTION, false);
    expect(second.clear).toBe(false);
  });

  test("auto-select resumes when the answer catches up", () => {
    const { paths, byPath } = rowset("readme.md");
    const stale = nextSearchSelection(INITIAL_SEARCH_SELECT, paths, byPath,
                                      EMPTY_SELECTION, false);
    const fresh = nextSearchSelection(stale.state, paths, byPath, EMPTY_SELECTION, true);
    expect(fresh.select).toBe("/d/readme.md");
  });
});
