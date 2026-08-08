// Pure bits of the selection model. The stateful half (the hook, the keyboard
// handler, the reconcile) needs a DOM and a React renderer, neither of which
// the frontend test setup has — see shortcut-chord.test.ts.
import { describe, expect, test } from "bun:test";
import { autoSelectPath, firstEntryPath, rangeBetween } from "./selection";

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

  test("a bare URL: the folder picks its own first entry", () => {
    const { paths, byPath } = folder();
    expect(autoSelectPath(null, paths, byPath)).toBe("/d/src");
  });

  test("a `?sel` seed owns the selection", () => {
    const { paths, byPath } = folder();
    expect(autoSelectPath("b.txt", paths, byPath)).toBeNull();
    // Even a `sel` naming no current row: the URL made a claim, and the seeding
    // effect is the one entitled to decide what to do about it.
    expect(autoSelectPath("gone.txt", paths, byPath)).toBeNull();
  });

  test("a bare URL wins over a selection that is about to be discarded", () => {
    // The regression this pins: browse into a file and come back to its folder.
    // The URL no longer carries `sel`, but the cross-remount stash still holds
    // the old selection — which the seeding effect is clearing on this very
    // commit. Reading that stale selection instead of the URL burned the one
    // shot and left the pane empty for the whole mount. Nothing about a stale
    // selection is an input here, which is how it stays fixed.
    const { paths, byPath } = folder();
    expect(autoSelectPath(null, paths, byPath)).toBe("/d/src");
  });

  test("an empty folder leaves the selection empty", () => {
    expect(autoSelectPath(null, [], new Map())).toBeNull();
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
