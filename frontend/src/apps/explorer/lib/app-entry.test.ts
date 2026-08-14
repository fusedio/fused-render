// The shell's copy of the entry rule (lib/app-entry.ts, D269). Every case here
// is a case `tests/test_shared_app_entry.py` also pins against the Python twin
// (`templates/shared/app_entry.py`): the pane and the templates must resolve one
// folder to ONE page, and the only way to keep two implementations honest is to
// ask them the same questions.
import { expect, test } from "bun:test";

import type { FsEntry } from "@platform/lib/api";
import { entryHtmlName, entryHtmlPath } from "./app-entry";

function entries(...spec: (string | [string, "dir"])[]): FsEntry[] {
  return spec.map((s) => {
    const [name, kind] = typeof s === "string" ? [s, "file"] : s;
    return { name, is_dir: kind === "dir", size: null, mtime: null };
  });
}

test("index.html wins over every other page", () => {
  expect(entryHtmlName(entries("about.html", "index.html", "zeta.html"))).toBe("index.html");
  // Case-insensitively, like the Python rule's `n.lower() == "index.html"`.
  expect(entryHtmlName(entries("about.html", "Index.HTML"))).toBe("Index.HTML");
});

test("with no index, the first page in NAME order wins", () => {
  // Deterministic, and deliberately not "whatever the listing hands back first":
  // the pane and the claude template read the same folder and must pick the same
  // page, so the order is sorted here rather than inherited from the server.
  expect(entryHtmlName(entries("zeta.html", "alpha.html", "mid.html"))).toBe("alpha.html");
  expect(entryHtmlName(entries("b.html").concat(entries("a.html")))).toBe("a.html");
});

test("a folder with no top-level page has no entry", () => {
  // The whole "this is still just a folder" branch: the preview pane falls back
  // to the embedded listing and a card opens the folder.
  expect(entryHtmlName(entries("notes.md", "data.csv"))).toBe(null);
  expect(entryHtmlName([])).toBe(null);
});

test("hidden pages, directories and .htm do not count", () => {
  // `.draft.html` is a work in progress, not the folder's face; a DIRECTORY
  // named `build.html` is an exported site tree, and handing /render a directory
  // renders nothing; `.htm` is excluded because the Python twin excludes it and
  // these two must agree to the letter. (`selection.ts` used to hold a
  // deliberately more permissive page test that accepted `.htm`; it went with the
  // folder auto-select it served, D275, so this rule now stands alone.)
  expect(entryHtmlName(entries(".draft.html", "real.html"))).toBe("real.html");
  expect(entryHtmlName(entries(["build.html", "dir"], "real.html"))).toBe("real.html");
  expect(entryHtmlName(entries(".hidden.html"))).toBe(null);
  expect(entryHtmlName(entries(["index.html", "dir"]))).toBe(null);
  expect(entryHtmlName(entries("page.htm"))).toBe(null);
});

test("the path form joins onto the folder, root-safe", () => {
  expect(entryHtmlPath("/w/local/demo", entries("index.html")))
    .toBe("/w/local/demo/index.html");
  // The filesystem root already ends in a slash — `//index.html` would be a
  // different path to the server (fs-actions.join is why this is not `dir + "/"`).
  expect(entryHtmlPath("/", entries("index.html"))).toBe("/index.html");
  expect(entryHtmlPath("/w/local/demo", entries("notes.md"))).toBe(null);
});
