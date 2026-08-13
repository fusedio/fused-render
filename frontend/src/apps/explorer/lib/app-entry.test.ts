// The shell's copy of the entry rule (lib/app-entry.ts, D269). Every case here
// is a case `tests/test_shared_app_entry.py` also pins against the Python twin
// (`templates/shared/app_entry.py`): the pane and the templates must resolve one
// folder to ONE page, and the only way to keep two implementations honest is to
// ask them the same questions.
import { expect, test } from "bun:test";

import type { FsEntry } from "@platform/lib/api";

// The rule itself is pure, but its module graph is not: `join` comes from
// fs-actions, which reaches router.ts, which reads `location` at MODULE scope.
// bun's test runtime has no DOM, so without this the file only passed when some
// OTHER suite in the run had already stubbed the global (globals are shared —
// see the same stub, and the same reason, in platform/lib/appEntry.test.ts).
// `??=` so whichever suite gets there first wins, and a shape real enough for
// router's init-time legacy-path rewrite to read. Static imports are hoisted
// above this, so app-entry comes in dynamically, after the stub.
(globalThis as { location?: unknown }).location ??= {
  pathname: "/",
  search: "",
  href: "http://localhost/",
};

const { entryHtmlName, entryHtmlPath, folderOpenTarget } = await import("./app-entry");

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
  // named `build.html` is an exported site tree (the shape D263 names), and
  // handing /render a directory renders nothing; `.htm` is excluded because the
  // Python twin excludes it and these two must agree to the letter — the
  // divergence with selection.ts's isPageRow is documented in both modules.
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

// ------------------------------------------- where a folder CARD's click goes

test("a folder card opens the folder's entry PAGE, as a file", () => {
  // D269 at the homepage surface, the same answer platform/lib/appEntry.ts's
  // openTargetFor gives an /apps hub card: the page, with the isDir hint false
  // so the destination paints the file scaffold before its stat resolves.
  expect(folderOpenTarget("/w/local/demo", entries("index.html", "notes.md"))).toEqual({
    path: "/w/local/demo/index.html",
    isDir: false,
  });
  // Two pages is not "ambiguous, open the folder" — the deterministic first
  // page by name is the answer everywhere else already (D269 widened the
  // server's narrower rule for exactly this shape).
  expect(folderOpenTarget("/w/local/demo", entries("zeta.html", "alpha.html"))).toEqual({
    path: "/w/local/demo/alpha.html",
    isDir: false,
  });
});

test("a folder with no page opens as the folder it is", () => {
  expect(folderOpenTarget("/w/repo", entries("README.md", ["src", "dir"]))).toEqual({
    path: "/w/repo",
    isDir: true,
  });
  expect(folderOpenTarget("/w/repo", entries())).toEqual({ path: "/w/repo", isDir: true });
});

test("an unresolved or unreadable listing keeps TODAY's folder navigation", () => {
  // `null` is "no listing in hand" — the card's first render, before its
  // /api/fs/list lands, and its last one if that list never lands (an
  // unreadable folder, a dead mount). Both must answer the FOLDER, because
  // that answer is also what the anchor's href is carrying at that moment: an
  // href may lag the resolution, it may never lie about it. No third state, no
  // spinner, no dead link — the card stays clickable throughout and degrades
  // to precisely the navigation it had before D269 reached this surface.
  expect(folderOpenTarget("/w/local/demo", null)).toEqual({
    path: "/w/local/demo",
    isDir: true,
  });
});

test("the root is a folder like any other", () => {
  // entryHtmlPath's root-safe join, seen through the target: `/index.html`,
  // never `//index.html`.
  expect(folderOpenTarget("/", entries("index.html"))).toEqual({
    path: "/index.html",
    isDir: false,
  });
});
