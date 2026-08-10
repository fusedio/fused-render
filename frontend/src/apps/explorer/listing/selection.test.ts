// Pure bits of the selection model. The stateful half (the hook, the keyboard
// handler, the reconcile) needs a DOM and a React renderer, neither of which
// the frontend test setup has — see shortcut-chord.test.ts.
import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import {
  EMPTY_SELECTION,
  autoSelectPath,
  firstEntryPath,
  oneSelected,
  pathFromSelParam,
  cameFromSelParam,
  rangeBetween,
  rowPressAction,
  selParam,
  selectionClaimed,
} from "./selection";

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

describe("autoSelectPath", () => {
  const folder = () =>
    rows([
      ["src", true],
      ["a.txt", false],
      ["b.txt", false],
    ]);

  test("the folder picks its own first entry, dirs included", () => {
    const { paths, byPath } = folder();
    expect(autoSelectPath(paths, byPath)).toBe("/d/src");
  });

  test("nothing in the URL can claim the selection", () => {
    // The `?sel` param is gone (useListingSelection documents why), so this
    // decision has no input but the rows: every folder open lands on its first
    // entry, whatever the address bar happens to carry.
    // (autoSelectPath takes no URL argument at all any more — that IS the pin.)
    const { paths, byPath } = folder();
    expect(autoSelectPath(paths, byPath)).toBe("/d/src");
  });

  test("an empty folder leaves the selection empty", () => {
    expect(autoSelectPath([], new Map())).toBeNull();
  });
});

describe("selectionClaimed", () => {
  test("a fresh folder claims nothing", () => {
    expect(selectionClaimed(EMPTY_SELECTION)).toBe(false);
  });

  test("one clicked row is a claim", () => {
    expect(selectionClaimed(oneSelected("/d/b.txt"))).toBe(true);
  });

  test("a multi-row selection is a claim", () => {
    expect(selectionClaimed({ paths: ["/d/a", "/d/b"], anchor: "/d/a", lead: "/d/b" })).toBe(true);
  });

  test("a leftover anchor with no rows is not a claim", () => {
    // Nothing is highlighted, so nothing outranks the auto-select: `paths` is
    // the whole question.
    expect(selectionClaimed({ paths: [], anchor: "/d/a", lead: "/d/a" })).toBe(false);
  });

  // The yield lives in Listing's auto-select effect (the effect owns WHEN;
  // autoSelectPath owns the answer and stays blind to the selection, D240), and
  // that effect needs a DOM and a React renderer this test setup does not have.
  // So the wiring is pinned at the source: the guard must run BEFORE the
  // selectOnly, or a click made in the provisional scaffold — carried across
  // the swap by recallSelection — is overwritten with row one the moment the
  // resolved listing settles.
  test("the auto-select effect yields to a claim before it selects", () => {
    const src = readFileSync(join(import.meta.dir, "../Listing.tsx"), "utf8");
    const guard = src.indexOf("if (selectionClaimed(sel)) return;");
    const auto = src.indexOf("const first = autoSelectPath(");
    expect(guard).toBeGreaterThan(-1);
    expect(auto).toBeGreaterThan(-1);
    expect(guard).toBeLessThan(auto);
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
