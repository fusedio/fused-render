import { expect, test } from "bun:test";
import {
  DEFAULT_LAYOUT,
  GRID_COLS,
  MAX_ROWS,
  SOURCES,
  addWidget,
  allowedSizes,
  canPlace,
  compactLayout,
  dims,
  emptyRows,
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
// x / y are half-cell units (a 1x1 is 2 x 2 units).
const lay = (...widgets: any[]): HomeLayout => ({ version: 4, widgets });
const ids = (l: HomeLayout) => l.widgets.map((x) => x.id);
const at = (l: HomeLayout) => l.widgets.map((x) => [x.x, x.y]);
// A v2 document (no coordinates) in the shape the packDense test uses.
const v2 = (sizes: string[]) => sizes.map((s, i) => ({ id: `w${i}`, source: "folder", folderId: "f", size: s, format: "list" }));

test("default layout stacks five full rows at x=0, y=0,2,4,6,8 (units), version 4", () => {
  expect(DEFAULT_LAYOUT.version).toBe(4);
  expect(DEFAULT_LAYOUT.widgets.map((x) => [x.source, x.size, x.format, x.x, x.y])).toEqual([
    ["search", "4x1", "bar", 0, 0],
    ["apps", "4x1", "cards", 0, 2],
    ["playground", "4x1", "cards", 0, 4],
    ["sessions", "4x1", "cards", 0, 6],
    ["recents", "4x1", "cards", 0, 8],
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
  expect(normalizeLayout({ version: 5, widgets: [] })).toEqual(DEFAULT_LAYOUT);
  expect(normalizeLayout({ version: 2, widgets: "x" })).toEqual(DEFAULT_LAYOUT);
});

test("normalizeLayout keeps an intentionally empty layout", () => {
  expect(normalizeLayout({ version: 2, widgets: [] }).widgets).toEqual([]);
  expect(normalizeLayout({ version: 3, widgets: [] }).widgets).toEqual([]);
  expect(normalizeLayout({ version: 4, widgets: [] }).widgets).toEqual([]);
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

test("packDense mirrors CSS row-dense on 8 units", () => {
  const sz = (...s: any[]) => s.map((size) => ({ size }));
  const pos = (items: { x: number; y: number }[]) => items.map((i) => [i.x, i.y]);
  expect(pos(packDense(sz("4x1", "2x2", "1x1", "1x1", "1x1", "4x1")))).toEqual([
    [0, 0],
    [0, 2],
    [4, 2],
    [6, 2],
    [4, 4],
    [0, 6],
  ]);
  expect(pos(packDense(sz("2x2", "4x1", "1x1")))).toEqual([
    [0, 0],
    [0, 4],
    [4, 0],
  ]);
});

test("packDense on 4 units clamps nothing; 4x1 input must be pre-clamped", () => {
  const out = packDense([{ size: "2x1" as const }, { size: "1x1" as const }, { size: "1x1" as const }], 4);
  expect(out.map((i) => [i.x, i.y])).toEqual([
    [0, 0],
    [0, 2],
    [2, 2],
  ]);
  // Contract: an 8-unit item cannot fit 4 units; the scan never finds a slot
  // and opens a row at x = 0 (overflowing). Callers clamp widths first.
  const wide = packDense([{ size: "4x1" as const }], 4);
  expect([wide[0].x, wide[0].y]).toEqual([0, 0]);
});

test("normalizeLayout migrates a v2 document with packDense and stamps 4", () => {
  const sizes = ["4x1", "2x2", "1x1", "1x1", "1x1", "4x1"];
  const want = [
    [0, 0],
    [0, 2],
    [4, 2],
    [6, 2],
    [4, 4],
    [0, 6],
  ];
  for (const version of [2, 1]) {
    const doc = version === 1 ? { version, widgets: v2(sizes) } : { version, widgets: v2(sizes) };
    const out = normalizeLayout(doc);
    expect(out.version).toBe(4);
    if (version === 2) {
      expect(at(out)).toEqual(want);
    } else {
      // v1 gets search prepended at (0,0), 4x1; everything else packs after it.
      expect(out.widgets[0]).toMatchObject({ source: "search", x: 0, y: 0 });
      expect(out.widgets).toHaveLength(7);
    }
  }
});

test("normalizeLayout v3 doubles cell coords to units, re-places overlapping or out-of-bounds ones with firstFreeSlot, sorts by (y,x)", () => {
  const out = normalizeLayout({
    version: 3,
    widgets: [
      w("a", "apps", "2x1", "cards", 2, 1),
      w("b", "apps", "2x1", "cards", 0, 0),
      w("c", "apps", "2x1", "cards", 3, 0), // out of bounds (3 + 2 > 4)
      w("d", "apps", "2x1", "cards", 2, 1), // overlaps a
    ],
  });
  expect(out.version).toBe(4);
  const by = Object.fromEntries(out.widgets.map((x) => [x.id, [x.x, x.y]]));
  expect(by.a).toEqual([4, 2]);
  expect(by.b).toEqual([0, 0]);
  expect(by.c).toEqual([4, 0]); // first free 2-wide slot, row-major
  expect(by.d).toEqual([0, 2]);
  expect(at(out)).toEqual([
    [0, 0],
    [4, 0],
    [0, 2],
    [4, 2],
  ]);
});

test("a v3 1x1 at cell x=1 migrates to unit x=2 at version 4", () => {
  const out = normalizeLayout({ version: 3, widgets: [w("a", "bots", "1x1", "list", 1, 0)] });
  expect(out.version).toBe(4);
  expect(at(out)).toEqual([[2, 0]]);
});

test("a v4 1x1 half a cell in (x=1) round-trips through normalizeLayout unchanged", () => {
  const doc = { version: 4, widgets: [w("a", "bots", "1x1", "list", 1, 0)] };
  const out = normalizeLayout(doc);
  expect(out.version).toBe(4);
  expect(out.widgets).toEqual(doc.widgets);
});

test("normalizeLayout drops x/y that are not integers", () => {
  const out = normalizeLayout({
    version: 4,
    widgets: [
      { ...w("a"), x: true, y: 0 },
      { ...w("b"), x: 0, y: 1.5 },
      { ...w("c"), x: "0", y: "3" },
      { ...w("d"), x: undefined, y: undefined },
    ],
  });
  expect(at(out)).toEqual([
    [0, 0],
    [0, 2],
    [0, 4],
    [0, 6],
  ]);
});

test("canPlace rejects out of bounds and overlap, ignores the excepted widget's own cells", () => {
  const l = lay(w("a", "apps", "2x2", "cards", 0, 0), w("b", "apps", "1x1", "cards", 6, 0));
  expect(canPlace(l.widgets, { x: 4, y: 0, cols: 2, rows: 2 })).toBe(true);
  expect(canPlace(l.widgets, { x: 2, y: 2, cols: 2, rows: 2 })).toBe(false);
  expect(canPlace(l.widgets, { x: 6, y: 0, cols: 4, rows: 2 })).toBe(false);
  expect(canPlace(l.widgets, { x: -1, y: 0, cols: 2, rows: 2 })).toBe(false);
  expect(canPlace(l.widgets, { x: 0, y: -1, cols: 2, rows: 2 })).toBe(false);
  expect(canPlace(l.widgets, { x: 0, y: 127, cols: 2, rows: 4 })).toBe(false);
  expect(canPlace(l.widgets, { x: 0, y: 2, cols: 4, rows: 4 }, "a")).toBe(true);
  expect(canPlace(l.widgets, { x: 0, y: 2, cols: 4, rows: 4 })).toBe(false);
  expect(occupancy(l.widgets, "a").size).toBe(4);
});

test("canPlace on half-cell units: a 1x1 cannot start at x=7, and x=1 / x=2 1x1s overlap", () => {
  expect(GRID_COLS).toBe(8);
  expect(canPlace([], { x: 7, y: 0, cols: 2, rows: 2 })).toBe(false);
  expect(canPlace([], { x: 6, y: 0, cols: 2, rows: 2 })).toBe(true);
  const l = lay(w("a", "apps", "1x1", "cards", 1, 0));
  expect(canPlace(l.widgets, { x: 2, y: 0, cols: 2, rows: 2 })).toBe(false); // units 1-2 vs 2-3
  expect(canPlace(l.widgets, { x: 3, y: 0, cols: 2, rows: 2 })).toBe(true);
});

test("firstFreeSlot scans row-major and opens a new row when nothing fits", () => {
  const l = lay(
    w("a", "apps", "2x1", "cards", 0, 0),
    w("b", "apps", "1x1", "cards", 4, 0),
    w("c", "apps", "4x1", "cards", 0, 2),
  );
  expect(firstFreeSlot(l.widgets, "1x1")).toEqual({ x: 6, y: 0 });
  expect(firstFreeSlot(l.widgets, "2x1")).toEqual({ x: 0, y: 4 });
  expect(firstFreeSlot(l.widgets, "4x1")).toEqual({ x: 0, y: 4 });
  expect(firstFreeSlot([], "2x2")).toEqual({ x: 0, y: 0 });
});

test("placeWidget moves to a free cell, refuses occupied and out of bounds by returning the same object, keeps (y,x) order", () => {
  const l = lay(w("a", "apps", "1x1", "cards", 0, 0), w("b", "apps", "1x1", "cards", 2, 0));
  const moved = placeWidget(l, "a", 6, 0);
  expect(moved).not.toBe(l);
  expect(ids(moved)).toEqual(["b", "a"]);
  expect(at(moved)).toEqual([
    [2, 0],
    [6, 0],
  ]);
  expect(placeWidget(l, "a", 1, 0)).toBe(l);
  expect(placeWidget(l, "a", 8, 0)).toBe(l);
  expect(placeWidget(l, "a", -1, 0)).toBe(l);
  expect(placeWidget(l, "zzz", 2, 2)).toBe(l);
  expect(placeWidget(l, "a", 0, 0)).toBe(l);
  expect(at(l)).toEqual([
    [0, 0],
    [2, 0],
  ]); // pure
});

test("moveByArrow steps one unit, skips over blocked units, stops at the bounds, Down can open a new row", () => {
  const l = lay(w("a", "apps", "1x1", "cards", 0, 0), w("b", "apps", "1x1", "cards", 2, 0));
  expect(at(moveByArrow(l, "b", "ArrowRight")).pop()).toEqual([3, 0]);
  expect(at(moveByArrow(l, "a", "ArrowRight"))).toEqual([
    [2, 0],
    [4, 0],
  ]);
  expect(moveByArrow(l, "a", "ArrowLeft")).toBe(l);
  expect(moveByArrow(l, "b", "ArrowUp")).toBe(l);
  const down = moveByArrow(l, "a", "ArrowDown");
  expect(down.widgets.find((x) => x.id === "a")).toMatchObject({ x: 0, y: 1 });
  expect(moveByArrow(l, "a", "Enter")).toBe(l);
});

test("setSize keeps the top-left anchor, clamps x for width, refuses when the new footprint overlaps; allowedSizes agrees", () => {
  const l = lay(w("a", "apps", "2x1", "cards", 4, 0), w("b", "apps", "1x1", "cards", 0, 2));
  const full = setSize(l, "a", "4x1");
  expect(full.widgets.find((x) => x.id === "a")).toMatchObject({ size: "4x1", x: 0, y: 0 });
  const tall = setSize(l, "a", "1x2");
  expect(tall.widgets.find((x) => x.id === "a")).toMatchObject({ size: "1x2", x: 4, y: 0 });
  // 2x2 at x=0 would cover b at (0,2)
  const blocked = lay(w("a", "apps", "2x1", "cards", 0, 0), w("b", "apps", "1x1", "cards", 0, 2));
  expect(setSize(blocked, "a", "2x2")).toBe(blocked);
  expect(setSize(blocked, "a", "1x2")).toBe(blocked);
  expect(allowedSizes(blocked, "a")).toEqual(["4x1", "2x1"]);
  expect(allowedSizes(l, "a")).toEqual(["4x1", "2x1", "1x2", "2x2"]);
  expect(setSize(l, "a", "2x1")).toBe(l);
  const ix = lay(w("c", "index", "1x1", "count"));
  expect(setSize(ix, "c", "1x2")).toBe(ix);
});

test("addWidget places at the first free slot (fills a hole before opening a row) and respects the 48 cap", () => {
  const l = lay(w("a", "apps", "2x1", "cards", 0, 0), w("b", "apps", "1x1", "cards", 6, 2));
  const out = addWidget(l, "bots", { id: "n" }); // 1x1
  expect(out.widgets.find((x) => x.id === "n")).toMatchObject({ x: 4, y: 0 });
  const wide = addWidget(l, "tasks", { id: "t" }); // 2x2 skips rows 0-1 (a) and b's columns, fits at (0,2)
  expect(wide.widgets.find((x) => x.id === "t")).toMatchObject({ x: 0, y: 2 });
  const full = lay(...Array.from({ length: 48 }, (_, i) => w(`w${i}`, "apps", "4x1", "cards", 0, i * 2)));
  expect(addWidget(full, "apps")).toBe(full);
});

test("addWidget appends with the source defaults", () => {
  const out = addWidget(lay(w("a")), "tasks", { id: "t" });
  expect(out.widgets[1]).toEqual({ id: "t", source: "tasks", size: "2x2", format: "list", x: 0, y: 2 });
  const f = addWidget(out, "folder", { id: "f", folderId: "bk1" });
  expect(f.widgets[2]).toEqual({ id: "f", source: "folder", size: "2x1", format: "list", folderId: "bk1", x: 4, y: 2 });
  expect(addWidget(out, "apps", { format: "icons" }).widgets[2].format).toBe("icons");
});

test("removeWidget leaves the hole", () => {
  const l = lay(w("a", "apps", "2x1", "cards", 0, 0), w("b", "apps", "2x1", "cards", 4, 0), w("c", "apps", "4x1", "cards", 0, 2));
  const out = removeWidget(l, "a");
  expect(ids(out)).toEqual(["b", "c"]);
  expect(at(out)).toEqual([
    [4, 0],
    [0, 2],
  ]);
  expect(removeWidget(l, "zzz")).toBe(l);
});

test("compactLayout equals packDense of reading order", () => {
  const l = lay(
    w("a", "apps", "2x1", "cards", 4, 6),
    w("b", "apps", "1x1", "cards", 0, 10),
    w("c", "apps", "4x1", "cards", 0, 14),
  );
  const out = compactLayout(l);
  expect(at(out)).toEqual(sortByPosition(packDense(sortByPosition(l.widgets))).map((x) => [x.x, x.y]));
  expect(at(out)).toEqual([
    [0, 0],
    [4, 0],
    [0, 2],
  ]);
  expect(ids(out)).toEqual(["a", "b", "c"]);
});

test("compactLayout lands widgets that sat on odd (half-cell) x on even x", () => {
  const l = lay(w("a", "apps", "1x1", "cards", 1, 0), w("b", "apps", "1x1", "cards", 5, 0), w("c", "apps", "1x1", "cards", 3, 4));
  const out = compactLayout(l);
  for (const x of out.widgets) expect(x.x % 2).toBe(0);
  expect(at(out)).toEqual([
    [0, 0],
    [2, 0],
    [4, 0],
  ]);
});

test("compactLayout is idempotent", () => {
  const l = lay(w("a", "apps", "2x2", "cards", 2, 8), w("b", "apps", "1x1", "cards", 6, 18), w("c", "apps", "4x1", "cards", 0, 24));
  const once = compactLayout(l);
  expect(compactLayout(once)).toEqual(once);
});

test("reflowToColumns(4) clamps 4x1 to 4 units wide, keeps reading order, packs densely", () => {
  const l = lay(
    w("a", "apps", "4x1", "cards", 0, 0),
    w("b", "apps", "1x1", "cards", 6, 2),
    w("c", "apps", "2x2", "cards", 0, 4),
  );
  const m = reflowToColumns(l, 4);
  expect([...m.values()].map((r) => [r.x, r.y])).toEqual([
    [0, 0],
    [0, 2],
    [0, 4],
  ]);
  expect(m.get("a")).toMatchObject({ cols: 4, rows: 2 });
  expect([...m.keys()]).toEqual(["a", "b", "c"]);
});

test("itemCapacity grows with size", () => {
  expect(dims("2x2")).toEqual({ cols: 4, rows: 4 });
  expect(dims("1x1")).toEqual({ cols: 2, rows: 2 });
  expect(dims("4x1")).toEqual({ cols: 8, rows: 2 });
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
  expect(out.version).toBe(4);
  expect(out.widgets.map((x) => x.source)).toEqual(["search", "apps"]);
  expect(out.widgets[0]).toMatchObject({ size: "4x1", format: "bar", x: 0, y: 0 });
  expect(out.widgets[1]).toMatchObject({ x: 0, y: 2 });
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
    y: 2,
  });
});

test("build widgets: addable, repeatable, and kept by normalizeLayout", () => {
  const out = addWidget(lay(w("a")), "build", { id: "b" });
  expect(out.widgets[1]).toEqual({ id: "b", source: "build", size: "4x1", format: "bar", x: 0, y: 2 });
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

test("normalizeLayout v4 repair never places a widget beyond MAX_ROWS", () => {
  // MAX_WIDGETS (48) caps how full the grid can get, so the MAX_ROWS drop in
  // the repair path is a defensive bound; assert the invariant holds anyway.
  const full = Array.from({ length: 47 }, (_, i) => w(`r${i}`, "apps", "4x1", "cards", 0, i * 2));
  const out = normalizeLayout({ version: 4, widgets: [...full, w("dup", "apps", "1x1", "cards", 0, 0)] });
  expect(out.widgets.length).toBe(48);
  for (const o of out.widgets) expect(o.y + dims(o.size).rows).toBeLessThanOrEqual(MAX_ROWS);
});

test("emptyRows lists wholly uncovered rows above the last occupied row", () => {
  const r = (y: number, rows = 1) => ({ y, rows });
  expect(emptyRows([r(0), r(2)])).toEqual([1]);
  expect(emptyRows([r(0), r(1), r(2)])).toEqual([]);
  expect(emptyRows([r(0), r(1, 2)])).toEqual([]);
  expect(emptyRows([r(0), r(3)])).toEqual([1, 2]);
  expect(emptyRows([])).toEqual([]);
  // Two 1x1s (2 unit rows each) at y=0 and y=3 leave unit row 2 uncovered.
  expect(emptyRows([r(0, 2), r(3, 2)])).toEqual([2]);
});
