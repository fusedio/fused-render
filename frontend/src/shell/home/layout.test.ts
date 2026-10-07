import { expect, test } from "bun:test";
import {
  DEFAULT_LAYOUT,
  SOURCES,
  addWidget,
  allowedSizes,
  canPlace,
  compactLayout,
  dims,
  firstFreeSlot,
  itemCapacity,
  moveByArrow,
  normalizeLayout,
  occupancy,
  packDense,
  placeWidget,
  reflowToColumns,
  removeWidget,
  setFormat,
  setSize,
  sortByPosition,
  type HomeLayout,
} from "./layout";
import { isWebUrl, normalizeWebUrl, pageTitle } from "./appPicker";

const w = (id: string, source: any = "apps", size: any = "4x1", format: any = "cards", x = 0, y = 0) => ({
  id,
  source,
  size,
  format,
  x,
  y,
});
const lay = (...widgets: any[]): HomeLayout => ({ version: 3, widgets });
const ids = (l: HomeLayout) => l.widgets.map((x) => x.id);
const at = (l: HomeLayout) => l.widgets.map((x) => [x.x, x.y]);
// A v2 document (no coordinates) in the shape the packDense test uses.
const v2 = (sizes: string[]) => sizes.map((s, i) => ({ id: `w${i}`, source: "folder", folderId: "f", size: s, format: "list" }));

test("default layout stacks five full rows at x=0, y=0..4, version 3", () => {
  expect(DEFAULT_LAYOUT.version).toBe(3);
  expect(DEFAULT_LAYOUT.widgets.map((x) => [x.source, x.size, x.format, x.x, x.y])).toEqual([
    ["search", "4x1", "bar", 0, 0],
    ["apps", "4x1", "cards", 0, 1],
    ["playground", "4x1", "cards", 0, 2],
    ["sessions", "4x1", "cards", 0, 3],
    ["recents", "4x1", "cards", 0, 4],
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
  expect(normalizeLayout({ version: 4, widgets: [] })).toEqual(DEFAULT_LAYOUT);
  expect(normalizeLayout({ version: 2, widgets: "x" })).toEqual(DEFAULT_LAYOUT);
});

test("normalizeLayout keeps an intentionally empty layout", () => {
  expect(normalizeLayout({ version: 2, widgets: [] }).widgets).toEqual([]);
  expect(normalizeLayout({ version: 3, widgets: [] }).widgets).toEqual([]);
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

test("packDense mirrors CSS row-dense on 4 columns", () => {
  const sz = (...s: any[]) => s.map((size) => ({ size }));
  const pos = (items: { x: number; y: number }[]) => items.map((i) => [i.x, i.y]);
  expect(pos(packDense(sz("4x1", "2x2", "1x1", "1x1", "1x1", "4x1")))).toEqual([
    [0, 0],
    [0, 1],
    [2, 1],
    [3, 1],
    [2, 2],
    [0, 3],
  ]);
  expect(pos(packDense(sz("2x2", "4x1", "1x1")))).toEqual([
    [0, 0],
    [0, 2],
    [2, 0],
  ]);
});

test("packDense on 2 columns clamps nothing; 4x1 input must be pre-clamped", () => {
  const out = packDense([{ size: "2x1" as const }, { size: "1x1" as const }, { size: "1x1" as const }], 2);
  expect(out.map((i) => [i.x, i.y])).toEqual([
    [0, 0],
    [0, 1],
    [1, 1],
  ]);
  // Contract: a 4-wide item cannot fit 2 columns; the scan never finds a slot
  // and opens a row at x = 0 (overflowing). Callers clamp widths first.
  const wide = packDense([{ size: "4x1" as const }], 2);
  expect([wide[0].x, wide[0].y]).toEqual([0, 0]);
});

test("normalizeLayout migrates a v2 document with packDense and stamps 3", () => {
  const sizes = ["4x1", "2x2", "1x1", "1x1", "1x1", "4x1"];
  const want = [
    [0, 0],
    [0, 1],
    [2, 1],
    [3, 1],
    [2, 2],
    [0, 3],
  ];
  for (const version of [2, 1]) {
    const doc = version === 1 ? { version, widgets: v2(sizes) } : { version, widgets: v2(sizes) };
    const out = normalizeLayout(doc);
    expect(out.version).toBe(3);
    if (version === 2) {
      expect(at(out)).toEqual(want);
    } else {
      // v1 gets search prepended at (0,0), 4x1; everything else packs after it.
      expect(out.widgets[0]).toMatchObject({ source: "search", x: 0, y: 0 });
      expect(out.widgets).toHaveLength(7);
    }
  }
});

test("normalizeLayout v3 keeps valid coords, re-places overlapping or out-of-bounds ones with firstFreeSlot, sorts by (y,x)", () => {
  const out = normalizeLayout({
    version: 3,
    widgets: [
      w("a", "apps", "2x1", "cards", 2, 1),
      w("b", "apps", "2x1", "cards", 0, 0),
      w("c", "apps", "2x1", "cards", 3, 0), // out of bounds (3 + 2 > 4)
      w("d", "apps", "2x1", "cards", 2, 1), // overlaps a
    ],
  });
  expect(out.version).toBe(3);
  const by = Object.fromEntries(out.widgets.map((x) => [x.id, [x.x, x.y]]));
  expect(by.a).toEqual([2, 1]);
  expect(by.b).toEqual([0, 0]);
  expect(by.c).toEqual([2, 0]); // first free 2-wide slot, row-major
  expect(by.d).toEqual([0, 1]);
  expect(at(out)).toEqual([
    [0, 0],
    [2, 0],
    [0, 1],
    [2, 1],
  ]);
});

test("normalizeLayout drops x/y that are not integers", () => {
  const out = normalizeLayout({
    version: 3,
    widgets: [
      { ...w("a"), x: true, y: 0 },
      { ...w("b"), x: 0, y: 1.5 },
      { ...w("c"), x: "0", y: "3" },
      { ...w("d"), x: undefined, y: undefined },
    ],
  });
  expect(at(out)).toEqual([
    [0, 0],
    [0, 1],
    [0, 2],
    [0, 3],
  ]);
});

test("canPlace rejects out of bounds and overlap, ignores the excepted widget's own cells", () => {
  const l = lay(w("a", "apps", "2x2", "cards", 0, 0), w("b", "apps", "1x1", "cards", 3, 0));
  expect(canPlace(l.widgets, { x: 2, y: 0, cols: 1, rows: 1 })).toBe(true);
  expect(canPlace(l.widgets, { x: 1, y: 1, cols: 1, rows: 1 })).toBe(false);
  expect(canPlace(l.widgets, { x: 3, y: 0, cols: 2, rows: 1 })).toBe(false);
  expect(canPlace(l.widgets, { x: -1, y: 0, cols: 1, rows: 1 })).toBe(false);
  expect(canPlace(l.widgets, { x: 0, y: -1, cols: 1, rows: 1 })).toBe(false);
  expect(canPlace(l.widgets, { x: 0, y: 63, cols: 1, rows: 2 })).toBe(false);
  expect(canPlace(l.widgets, { x: 0, y: 1, cols: 2, rows: 2 }, "a")).toBe(true);
  expect(canPlace(l.widgets, { x: 0, y: 1, cols: 2, rows: 2 })).toBe(false);
  expect(occupancy(l.widgets, "a").size).toBe(1);
});

test("firstFreeSlot scans row-major and opens a new row when nothing fits", () => {
  const l = lay(
    w("a", "apps", "2x1", "cards", 0, 0),
    w("b", "apps", "1x1", "cards", 2, 0),
    w("c", "apps", "4x1", "cards", 0, 1),
  );
  expect(firstFreeSlot(l.widgets, "1x1")).toEqual({ x: 3, y: 0 });
  expect(firstFreeSlot(l.widgets, "2x1")).toEqual({ x: 0, y: 2 });
  expect(firstFreeSlot(l.widgets, "4x1")).toEqual({ x: 0, y: 2 });
  expect(firstFreeSlot([], "2x2")).toEqual({ x: 0, y: 0 });
});

test("placeWidget moves to a free cell, refuses occupied and out of bounds by returning the same object, keeps (y,x) order", () => {
  const l = lay(w("a", "apps", "1x1", "cards", 0, 0), w("b", "apps", "1x1", "cards", 1, 0));
  const moved = placeWidget(l, "a", 3, 0);
  expect(moved).not.toBe(l);
  expect(ids(moved)).toEqual(["b", "a"]);
  expect(at(moved)).toEqual([
    [1, 0],
    [3, 0],
  ]);
  expect(placeWidget(l, "a", 1, 0)).toBe(l);
  expect(placeWidget(l, "a", 4, 0)).toBe(l);
  expect(placeWidget(l, "a", -1, 0)).toBe(l);
  expect(placeWidget(l, "zzz", 2, 2)).toBe(l);
  expect(placeWidget(l, "a", 0, 0)).toBe(l);
  expect(at(l)).toEqual([
    [0, 0],
    [1, 0],
  ]); // pure
});

test("moveByArrow steps one cell, skips over blocked cells, stops at the bounds, Down can open a new row", () => {
  const l = lay(w("a", "apps", "1x1", "cards", 0, 0), w("b", "apps", "1x1", "cards", 1, 0));
  expect(at(moveByArrow(l, "b", "ArrowRight")).pop()).toEqual([2, 0]);
  expect(at(moveByArrow(l, "a", "ArrowRight"))).toEqual([
    [1, 0],
    [2, 0],
  ]);
  expect(moveByArrow(l, "a", "ArrowLeft")).toBe(l);
  expect(moveByArrow(l, "b", "ArrowUp")).toBe(l);
  const down = moveByArrow(l, "a", "ArrowDown");
  expect(down.widgets.find((x) => x.id === "a")).toMatchObject({ x: 0, y: 1 });
  expect(moveByArrow(l, "a", "Enter")).toBe(l);
});

test("setSize keeps the top-left anchor, clamps x for width, refuses when the new footprint overlaps; allowedSizes agrees", () => {
  const l = lay(w("a", "apps", "2x1", "cards", 2, 0), w("b", "apps", "1x1", "cards", 0, 1));
  const full = setSize(l, "a", "4x1");
  expect(full.widgets.find((x) => x.id === "a")).toMatchObject({ size: "4x1", x: 0, y: 0 });
  const tall = setSize(l, "a", "1x2");
  expect(tall.widgets.find((x) => x.id === "a")).toMatchObject({ size: "1x2", x: 2, y: 0 });
  // 2x2 at x=0 would cover b at (0,1)
  const blocked = lay(w("a", "apps", "2x1", "cards", 0, 0), w("b", "apps", "1x1", "cards", 0, 1));
  expect(setSize(blocked, "a", "2x2")).toBe(blocked);
  expect(setSize(blocked, "a", "1x2")).toBe(blocked);
  expect(allowedSizes(blocked, "a")).toEqual(["4x1", "2x1"]);
  expect(allowedSizes(l, "a")).toEqual(["4x1", "2x1", "1x2", "2x2"]);
  expect(setSize(l, "a", "2x1")).toBe(l);
  const ix = lay(w("c", "index", "1x1", "count"));
  expect(setSize(ix, "c", "1x2")).toBe(ix);
});

test("addWidget places at the first free slot (fills a hole before opening a row) and respects the 48 cap", () => {
  const l = lay(w("a", "apps", "2x1", "cards", 0, 0), w("b", "apps", "1x1", "cards", 3, 1));
  const out = addWidget(l, "bots", { id: "n" }); // 1x1
  expect(out.widgets.find((x) => x.id === "n")).toMatchObject({ x: 2, y: 0 });
  const wide = addWidget(l, "tasks", { id: "t" }); // 2x2 skips row 0 (a is there), fits at (0,1)
  expect(wide.widgets.find((x) => x.id === "t")).toMatchObject({ x: 0, y: 1 });
  const full = lay(...Array.from({ length: 48 }, (_, i) => w(`w${i}`, "apps", "4x1", "cards", 0, i)));
  expect(addWidget(full, "apps")).toBe(full);
});

test("addWidget appends with the source defaults", () => {
  const out = addWidget(lay(w("a")), "tasks", { id: "t" });
  expect(out.widgets[1]).toEqual({ id: "t", source: "tasks", size: "2x2", format: "list", x: 0, y: 1 });
  const f = addWidget(out, "folder", { id: "f", folderId: "bk1" });
  expect(f.widgets[2]).toEqual({ id: "f", source: "folder", size: "2x1", format: "list", folderId: "bk1", x: 2, y: 1 });
  expect(addWidget(out, "apps", { format: "icons" }).widgets[2].format).toBe("icons");
});

test("removeWidget leaves the hole", () => {
  const l = lay(w("a", "apps", "2x1", "cards", 0, 0), w("b", "apps", "2x1", "cards", 2, 0), w("c", "apps", "4x1", "cards", 0, 1));
  const out = removeWidget(l, "a");
  expect(ids(out)).toEqual(["b", "c"]);
  expect(at(out)).toEqual([
    [2, 0],
    [0, 1],
  ]);
  expect(removeWidget(l, "zzz")).toBe(l);
});

test("compactLayout equals packDense of reading order", () => {
  const l = lay(
    w("a", "apps", "2x1", "cards", 2, 3),
    w("b", "apps", "1x1", "cards", 0, 5),
    w("c", "apps", "4x1", "cards", 0, 7),
  );
  const out = compactLayout(l);
  expect(at(out)).toEqual(sortByPosition(packDense(sortByPosition(l.widgets))).map((x) => [x.x, x.y]));
  expect(at(out)).toEqual([
    [0, 0],
    [2, 0],
    [0, 1],
  ]);
  expect(ids(out)).toEqual(["a", "b", "c"]);
});

test("compactLayout is idempotent", () => {
  const l = lay(w("a", "apps", "2x2", "cards", 1, 4), w("b", "apps", "1x1", "cards", 3, 9), w("c", "apps", "4x1", "cards", 0, 12));
  const once = compactLayout(l);
  expect(compactLayout(once)).toEqual(once);
});

test("reflowToColumns(2) clamps 4x1 to 2 wide, keeps reading order, packs densely", () => {
  const l = lay(
    w("a", "apps", "4x1", "cards", 0, 0),
    w("b", "apps", "1x1", "cards", 3, 1),
    w("c", "apps", "2x2", "cards", 0, 2),
  );
  const m = reflowToColumns(l, 2);
  expect([...m.values()].map((r) => [r.x, r.y])).toEqual([
    [0, 0],
    [0, 1],
    [0, 2],
  ]);
  expect(m.get("a")).toMatchObject({ cols: 2, rows: 1 });
  expect([...m.keys()]).toEqual(["a", "b", "c"]);
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

test("setFormat", () => {
  const l = lay(w("a"), w("b", "tasks", "2x2", "list", 0, 1));
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
  expect(out.widgets[0]).toEqual({ id: "b", source: "app", size: "2x1", format: "live", appPath: "/w/x", x: 0, y: 0 });
  expect(out.widgets[1].appPath).toBeUndefined();
});

test("addWidget stores appPath for app widgets", () => {
  const out = addWidget(lay(), "app", { id: "p", appPath: "/w/x" });
  expect(out.widgets[0]).toEqual({ id: "p", source: "app", size: "2x2", format: "live", appPath: "/w/x", x: 0, y: 0 });
});

test("a version-1 layout gets search prepended and is stamped current", () => {
  const out = normalizeLayout({ version: 1, widgets: [w("a")] });
  expect(out.version).toBe(3);
  expect(out.widgets.map((x) => x.source)).toEqual(["search", "apps"]);
  expect(out.widgets[0]).toMatchObject({ size: "4x1", format: "bar", x: 0, y: 0 });
  expect(out.widgets[1]).toMatchObject({ x: 0, y: 1 });
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
  expect(addWidget(lay(w("a")), "search", { id: "s" }).widgets[1]).toEqual({
    id: "s",
    source: "search",
    size: "4x1",
    format: "bar",
    x: 0,
    y: 1,
  });
});

test("build widgets: addable, repeatable, and kept by normalizeLayout", () => {
  const out = addWidget(lay(w("a")), "build", { id: "b" });
  expect(out.widgets[1]).toEqual({ id: "b", source: "build", size: "4x1", format: "bar", x: 0, y: 1 });
  expect(addWidget(out, "build", { id: "b2" }).widgets).toHaveLength(3);
  expect(normalizeLayout({ version: 2, widgets: [w("b", "build", "2x1", "bar")] }).widgets).toEqual([
    { id: "b", source: "build", size: "2x1", format: "bar", x: 0, y: 0 },
  ]);
});

test("a 1x1 search widget is kept by normalizeLayout", () => {
  expect(normalizeLayout({ version: 2, widgets: [w("s", "search", "1x1", "bar")] }).widgets).toEqual([
    { id: "s", source: "search", size: "1x1", format: "bar", x: 0, y: 0 },
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
