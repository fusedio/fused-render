import { expect, test } from "bun:test";
import {
  DEFAULT_LAYOUT,
  SOURCES,
  addWidget,
  dims,
  itemCapacity,
  moveWidget,
  normalizeLayout,
  removeWidget,
  setFormat,
  setSize,
  type HomeLayout,
} from "./layout";
import { isWebUrl, normalizeWebUrl, pageTitle } from "./appPicker";

const w = (id: string, source: any = "apps", size: any = "4x1", format: any = "cards") => ({
  id,
  source,
  size,
  format,
});
const lay = (...widgets: any[]): HomeLayout => ({ version: 2, widgets });
const ids = (l: HomeLayout) => l.widgets.map((x) => x.id);

test("default layout is the search bar then today's four strips in order", () => {
  expect(DEFAULT_LAYOUT.widgets.map((x) => [x.source, x.size, x.format])).toEqual([
    ["search", "4x1", "bar"],
    ["apps", "4x1", "cards"],
    ["playground", "4x1", "cards"],
    ["sessions", "4x1", "cards"],
    ["recents", "4x1", "cards"],
  ]);
});

test("every source declares sizes and formats; spec defaults hold", () => {
  for (const s of Object.values(SOURCES)) {
    expect(s.sizes.length).toBeGreaterThan(0);
    expect(s.formats.length).toBeGreaterThan(0);
  }
  expect(SOURCES.tasks.sizes[0]).toBe("2x2");
  expect(SOURCES.folder.sizes[0]).toBe("2x1");
  expect(SOURCES.build.sizes[0]).toBe("4x1");
  expect(SOURCES.search.sizes[0]).toBe("4x1");
  expect(SOURCES.search.sizes).toContain("1x1");
});

test("normalizeLayout falls back to default on garbage", () => {
  expect(normalizeLayout(null)).toEqual(DEFAULT_LAYOUT);
  expect(normalizeLayout({ version: 3, widgets: [] })).toEqual(DEFAULT_LAYOUT);
  expect(normalizeLayout({ version: 2, widgets: "x" })).toEqual(DEFAULT_LAYOUT);
});

test("normalizeLayout keeps an intentionally empty layout", () => {
  expect(normalizeLayout({ version: 2, widgets: [] }).widgets).toEqual([]);
});

test("normalizeLayout drops unknown sources and folder widgets with no folderId", () => {
  const out = normalizeLayout({
    version: 2,
    widgets: [
      w("a"),
      w("b", "nope"),
      w("c", "folder", "2x1", "list"),
      { ...w("d", "folder", "2x1", "list"), folderId: "f" },
      5,
    ],
  });
  expect(ids(out)).toEqual(["a", "d"]);
});

test("normalizeLayout clamps disallowed size/format to the source default", () => {
  const out = normalizeLayout({
    version: 2,
    widgets: [w("a", "index", "4x1", "cards"), w("b", "tasks", "1x1", "icons")],
  });
  expect(out.widgets[0]).toMatchObject({ size: "1x1", format: "count" });
  expect(out.widgets[1]).toMatchObject({ size: "2x2", format: "list" });
});

test("normalizeLayout gives duplicate/missing ids fresh unique ids", () => {
  const out = normalizeLayout({ version: 2, widgets: [w("a"), w("a"), { ...w("x"), id: undefined }] });
  expect(new Set(ids(out)).size).toBe(3);
});

test("moveWidget reorders and is pure", () => {
  const l = lay(w("a"), w("b"), w("c"));
  expect(ids(moveWidget(l, 0, 2))).toEqual(["b", "c", "a"]);
  expect(ids(moveWidget(l, 2, 0))).toEqual(["c", "a", "b"]);
  expect(ids(l)).toEqual(["a", "b", "c"]);
  expect(moveWidget(l, 1, 1)).toBe(l);
  expect(moveWidget(l, -1, 1)).toBe(l);
  expect(moveWidget(l, 0, 9)).toBe(l);
});

test("addWidget appends with the source defaults", () => {
  const out = addWidget(lay(w("a")), "tasks", { id: "t" });
  expect(out.widgets[1]).toEqual({ id: "t", source: "tasks", size: "2x2", format: "list" });
  const f = addWidget(out, "folder", { id: "f", folderId: "bk1" });
  expect(f.widgets[2]).toEqual({ id: "f", source: "folder", size: "2x1", format: "list", folderId: "bk1" });
  expect(addWidget(out, "apps", { format: "icons" }).widgets[2].format).toBe("icons");
});

test("addWidget respects the 48 widget cap", () => {
  const full = lay(...Array.from({ length: 48 }, (_, i) => w(`w${i}`)));
  expect(addWidget(full, "apps")).toBe(full);
});

test("itemCapacity grows with size", () => {
  expect(dims("2x2")).toEqual({ cols: 2, rows: 2 });
  expect(itemCapacity("2x1", "list")).toBe(2);
  expect(itemCapacity("2x2", "list")).toBe(6);
  expect(itemCapacity("4x1", "list")).toBe(4);
  expect(itemCapacity("2x1", "icons")).toBe(6);
  expect(itemCapacity("4x1", "icons")).toBe(12);
  expect(itemCapacity("1x2", "list")).toBe(6);
  expect(itemCapacity("1x2", "icons")).toBe(6);
});

test("removeWidget / setSize / setFormat", () => {
  const l = lay(w("a"), w("b", "tasks", "2x2", "list"));
  expect(ids(removeWidget(l, "a"))).toEqual(["b"]);
  expect(setSize(l, "b", "4x1").widgets[1].size).toBe("4x1");
  expect(setSize(l, "b", "1x1")).toBe(l);
  expect(setSize(l, "b", "1x2").widgets[1].size).toBe("1x2");
  const ix = lay(w("c", "index", "4x1", "cards"));
  expect(setSize(ix, "c", "1x2")).toBe(ix);
  expect(setFormat(l, "b", "board").widgets[1].format).toBe("board");
  expect(setFormat(l, "b", "cards")).toBe(l);
  expect(setFormat(l, "zzz", "board")).toBe(l);
});

test("app widgets need an appPath and keep it; other sources drop it", () => {
  const out = normalizeLayout({
    version: 2,
    widgets: [
      w("a", "app", "2x2", "live"),
      { ...w("b", "app", "2x1", "live"), appPath: "/w/x" },
      { ...w("c", "apps"), appPath: "/w/x" },
    ],
  });
  expect(ids(out)).toEqual(["b", "c"]);
  expect(out.widgets[0]).toEqual({ id: "b", source: "app", size: "2x1", format: "live", appPath: "/w/x" });
  expect(out.widgets[1].appPath).toBeUndefined();
});

test("addWidget stores appPath for app widgets", () => {
  const out = addWidget(lay(), "app", { id: "p", appPath: "/w/x" });
  expect(out.widgets[0]).toEqual({ id: "p", source: "app", size: "2x2", format: "live", appPath: "/w/x" });
});

test("a version-1 layout gets search prepended and is stamped current", () => {
  const out = normalizeLayout({ version: 1, widgets: [w("a")] });
  expect(out.version).toBe(2);
  expect(out.widgets.map((x) => x.source)).toEqual(["search", "apps"]);
  expect(out.widgets[0]).toMatchObject({ size: "4x1", format: "bar" });
  expect(normalizeLayout({ version: 1, widgets: [] }).widgets.map((x) => x.source)).toEqual(["search"]);
});

test("a version-1 layout that somehow has search keeps just that one", () => {
  const out = normalizeLayout({ version: 1, widgets: [w("a"), w("s", "search", "2x1", "bar")] });
  expect(out.widgets.map((x) => x.id)).toEqual(["a", "s"]);
});

test("a current layout without search stays without it", () => {
  expect(normalizeLayout({ version: 2, widgets: [w("a")] }).widgets.map((x) => x.source)).toEqual(["apps"]);
});

test("only one search widget: duplicates dropped, add is a no-op", () => {
  const out = normalizeLayout({ version: 2, widgets: [w("s1", "search", "4x1", "bar"), w("s2", "search", "2x1", "bar")] });
  expect(ids(out)).toEqual(["s1"]);
  expect(addWidget(out, "search")).toBe(out);
  expect(addWidget(lay(w("a")), "search", { id: "s" }).widgets[1]).toEqual({ id: "s", source: "search", size: "4x1", format: "bar" });
});

test("build widgets: addable, repeatable, and kept by normalizeLayout", () => {
  const out = addWidget(lay(w("a")), "build", { id: "b" });
  expect(out.widgets[1]).toEqual({ id: "b", source: "build", size: "4x1", format: "bar" });
  expect(addWidget(out, "build", { id: "b2" }).widgets).toHaveLength(3);
  expect(normalizeLayout({ version: 2, widgets: [w("b", "build", "2x1", "bar")] }).widgets).toEqual([
    { id: "b", source: "build", size: "2x1", format: "bar" },
  ]);
});

test("a 1x1 search widget is kept by normalizeLayout", () => {
  expect(normalizeLayout({ version: 2, widgets: [w("s", "search", "1x1", "bar")] }).widgets).toEqual([
    { id: "s", source: "search", size: "1x1", format: "bar" },
  ]);
});

test("pageTitle is the last path segment", () => {
  expect(pageTitle("/a/b/c.md")).toBe("c.md");
  expect(pageTitle("/a/b/")).toBe("b");
  expect(pageTitle("C:\\x\\y.html")).toBe("y.html");
  expect(pageTitle("/")).toBe("/");
});

test("web URLs: detection, normalisation, title, and normalize keeps the widget", () => {
  expect(isWebUrl("https://a.b/x")).toBe(true);
  expect(isWebUrl("/a/b")).toBe(false);
  expect(isWebUrl("ftp://x")).toBe(false);
  expect(normalizeWebUrl(" example.com/foo ")).toBe("https://example.com/foo");
  expect(normalizeWebUrl("not a url")).toBeNull();
  expect(normalizeWebUrl("http://x.y")).toBe("http://x.y");
  expect(pageTitle("https://www.example.com/a/b")).toBe("example.com");
  const out = normalizeLayout({
    version: 2,
    widgets: [{ ...w("u", "app", "2x2", "live"), appPath: "https://example.com" }],
  });
  expect(out.widgets[0].appPath).toBe("https://example.com");
});
