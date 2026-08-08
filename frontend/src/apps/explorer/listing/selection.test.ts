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
