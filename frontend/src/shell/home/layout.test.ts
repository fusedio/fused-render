import { expect, test } from "bun:test";
import {
  DEFAULT_LAYOUT,
  FIXED_ROWS,
  FORMAT_MIN_ROWS,
  sizesFor,
  formatForRows,
  GRID_COLS,
  MAX_ROWS,
  MAX_WIDGET_ROWS,
  PRESETS,
  defaultFormat,
  SOURCES,
  addWidget,
  allowedSizes,
  canPlace,
  collapseShrunkRows,
  contentSizeFor,
  defaultLayout,
  dims,
  dimsOf,
  emptySlots,
  fillSlot,
  firstFreeSlot,
  itemCapacity,
  matchPreset,
  minFootprint,
  normalizeLayout,
  occupancy,
  packDense,
  presetLayout,
  presetFor,
  rectOf,
  reflowToColumns,
  removeWidget,
  rowsUsed,
  setFormat,
  setSort,
  setTasksShow,
  setSize,
  sourceFits,
  swapSource,
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
const lay = (...widgets: any[]): HomeLayout => ({ version: 5, widgets });
const ids = (l: HomeLayout) => l.widgets.map((x) => x.id);
const at = (l: HomeLayout) => l.widgets.map((x) => [x.x, x.y]);
// A v2 document (no coordinates) in the shape the packDense test uses.
const v2 = (sizes: string[]) => sizes.map((s, i) => ({ id: `w${i}`, source: "folder", folderId: "f", size: s, format: "list" }));

test("default layout is the Builder preset (units), version 5, stable default-<source> ids", () => {
  expect(DEFAULT_LAYOUT.version).toBe(5);
  expect(DEFAULT_LAYOUT.widgets.map((x) => [x.source, x.size, x.x, x.y, x.format])).toEqual([
    ["search", "4x1", 0, 0, "bar"],
    ["build", "2x1", 0, 1, SOURCES.build.formats[0]],
    ["apps", "2x2", 4, 1, "icons"],
    ["bots", "2x2", 0, 5, "list"],
    ["tasks", "2x2", 4, 5, "list"],
  ]);
  expect(ids(DEFAULT_LAYOUT)).toEqual(["default-search", "default-build", "default-apps", "default-bots", "default-tasks"]);
});

test("defaultLayout has no overlaps", () => {
  const l = defaultLayout();
  expect(l.widgets.every((x) => canPlace(l.widgets, rectOf(x), x.id))).toBe(true);
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
  expect(normalizeLayout({ version: 6, widgets: [] })).toEqual(DEFAULT_LAYOUT);
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
  const sz = (...s: any[]) => s.map((size) => ({ source: "bots" as const, size }));
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
  const out = packDense([{ source: "bots" as const, size: "2x1" as const }, { source: "bots" as const, size: "1x1" as const }, { source: "bots" as const, size: "1x1" as const }], 4);
  expect(out.map((i) => [i.x, i.y])).toEqual([
    [0, 0],
    [0, 2],
    [2, 2],
  ]);
  // Contract: an 8-unit item cannot fit 4 units; the scan never finds a slot
  // and opens a row at x = 0 (overflowing). Callers clamp widths first.
  const wide = packDense([{ source: "bots" as const, size: "4x1" as const }], 4);
  expect([wide[0].x, wide[0].y]).toEqual([0, 0]);
});

test("normalizeLayout migrates a v2 document with packDense and stamps 5", () => {
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
    expect(out.version).toBe(5);
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
  expect(out.version).toBe(5);
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

test("a v3 1x1 at cell x=1 migrates to unit x=2 at version 5", () => {
  const out = normalizeLayout({ version: 3, widgets: [w("a", "bots", "1x1", "list", 1, 0)] });
  expect(out.version).toBe(5);
  expect(at(out)).toEqual([[2, 0]]);
});

test("a v4 1x1 half a cell in (x=1) round-trips through normalizeLayout unchanged", () => {
  const doc = { version: 4, widgets: [w("a", "bots", "1x1", "list", 1, 0)] };
  const out = normalizeLayout(doc);
  expect(out.version).toBe(5);
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
  expect(firstFreeSlot(l.widgets, "apps", "1x1")).toEqual({ x: 6, y: 0 });
  expect(firstFreeSlot(l.widgets, "apps", "2x1")).toEqual({ x: 0, y: 4 });
  expect(firstFreeSlot(l.widgets, "apps", "4x1")).toEqual({ x: 0, y: 4 });
  expect(firstFreeSlot([], "apps", "2x2")).toEqual({ x: 0, y: 0 });
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
  expect(itemCapacity("2x2", "list")).toBe(4);
  expect(itemCapacity("4x1", "list")).toBe(4);
  expect(itemCapacity("2x1", "icons")).toBe(6);
  expect(itemCapacity("4x1", "icons")).toBe(12);
  expect(itemCapacity("1x2", "list")).toBe(4);
  expect(itemCapacity("1x2", "icons")).toBe(6);
});

test("setFormat", () => {
  const l = lay(w("a"), w("b", "tasks", "2x2", "list", 0, 1));
  expect(setFormat(l, "b", "board").widgets[1].format).toBe("board");
  expect(setFormat(l, "b", "cards")).toBe(l);
  expect(setFormat(l, "zzz", "board")).toBe(l);
});

test("setSort", () => {
  const l = lay(w("a", "apps"), w("t", "tasks", "2x2", "list", 0, 1));
  expect(setSort(l, "a", "name").widgets[0].sort).toBe("name");
  const named = setSort(l, "a", "name");
  expect(setSort(named, "a", "name")).toBe(named);
  expect(setSort(l, "a", "opened")).toBe(l);
  expect(setSort(l, "zzz", "name")).toBe(l);
  expect(setSort(l, "t", "name")).toBe(l);
});

test("setTasksShow", () => {
  const l = lay(w("a", "apps"), w("t", "tasks", "2x2", "list", 0, 1));
  const closed = setTasksShow(l, "t", "open");
  expect(closed.widgets[1].show).toBe("open");
  expect(setTasksShow(closed, "t", "open")).toBe(closed);
  expect(setTasksShow(l, "t", "open_done")).toBe(l);
  expect(setTasksShow(l, "a", "open")).toBe(l);
  expect(setTasksShow(l, "zzz", "open")).toBe(l);
});

test("show round-trips through normalizeLayout for tasks tiles only; addWidget too", () => {
  const out = normalizeLayout({
    version: 2,
    widgets: [
      { ...w("a", "tasks"), show: "open" },
      { ...w("b", "apps"), show: "open" },
      { ...w("c", "tasks"), show: "zzz" },
    ],
  });
  expect(out.widgets.find((x) => x.id === "a")?.show).toBe("open");
  expect(out.widgets.find((x) => x.id === "b")?.show).toBeUndefined();
  expect(out.widgets.find((x) => x.id === "c")?.show).toBeUndefined();
  expect(addWidget(lay(), "tasks", { id: "t", show: "open" }).widgets[0].show).toBe("open");
  expect(addWidget(lay(), "apps", { id: "p", show: "open" }).widgets[0].show).toBeUndefined();
});

test("normalizeLayout keeps sort on apps widgets only", () => {
  const out = normalizeLayout({
    version: 2,
    widgets: [
      { ...w("a", "apps"), sort: "name" },
      { ...w("b", "recents"), sort: "name" },
      { ...w("c", "apps"), sort: "zzz" },
    ],
  });
  expect(out.widgets.find((x) => x.id === "a")?.sort).toBe("name");
  expect(out.widgets.find((x) => x.id === "b")?.sort).toBeUndefined();
  expect(out.widgets.find((x) => x.id === "c")?.sort).toBeUndefined();
});

test("addWidget stores sort for apps widgets only", () => {
  expect(addWidget(lay(), "apps", { id: "a", sort: "updated" }).widgets[0].sort).toBe("updated");
  expect(addWidget(lay(), "recents", { id: "r", sort: "updated" }).widgets[0].sort).toBeUndefined();
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
  expect(out.version).toBe(5);
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

// ---- Edge resize (explicit cols/rows footprint) -----------------------------

const solo = (extra: any = {}, source: any = "bots", size: any = "1x1") =>
  lay({ ...w("a", source, size, "cards", 0, 0), ...extra });
const only = (l: HomeLayout) => l.widgets.find((x) => x.id === "a")!;

test("dimsOf falls back to dims(size) and honours cols/rows", () => {
  expect(dimsOf({ source: "bots", size: "2x1" })).toEqual({ cols: 4, rows: 2 });
  expect(dimsOf({ source: "bots", size: "1x1", cols: 3, rows: 2 })).toEqual({ cols: 3, rows: 2 });
});

test("search and build have a fixed height; presets mean width only", () => {
  expect(dimsOf({ source: "search", size: "4x1" })).toEqual({ cols: 8, rows: 1 });
  expect(dimsOf({ source: "search", size: "4x1", cols: 4, rows: 2 })).toEqual({ cols: 4, rows: 1 });
  expect(dimsOf({ source: "build", size: "2x1" })).toEqual({ cols: 4, rows: 4 });
  expect(minFootprint("search")).toEqual({ cols: 2, rows: 1 });
  expect(minFootprint("build")).toEqual({ cols: 4, rows: 4 });
  expect(minFootprint("apps")).toEqual({ cols: 2, rows: 2 });
});

test("presetFor and allowedSizes treat search width only", () => {
  expect(presetFor("search", 4, 1)).toBe("2x1");
  expect(presetFor("search", 8, 3)).toBe("4x1");
  const l = lay(w("s", "search", "1x1", "bar", 0, 0), w("a", "apps", "4x1", "cards", 0, 1));
  expect(allowedSizes(l, "s")).toEqual(["4x1", "2x1", "1x1"]);
});

test("normalizeLayout grows a v5 build stored at 3 rows to 4 and pushes what sits below it down", () => {
  const out = normalizeLayout({
    version: 5,
    widgets: [
      w("s", "search", "4x1", "bar", 0, 0),
      { ...w("b", "build", "4x1", "bar", 0, 1), cols: 8, rows: 3 },
      w("a", "apps", "4x1", "cards", 0, 4),
    ],
  });
  const b = out.widgets.find((x) => x.id === "b")!;
  expect(b.y).toBe(1);
  expect(dimsOf(b).rows).toBe(4);
  expect(out.widgets.find((x) => x.id === "a")!.y).toBe(5);
});

test("normalizeLayout v4 -> v5 shrinks search/build and collapses only the vacated rows", () => {
  const out = normalizeLayout({
    version: 4,
    widgets: [w("s", "search", "4x1", "bar", 0, 0), w("a", "apps", "4x1", "cards", 0, 2)],
  });
  expect(out.version).toBe(5);
  expect(dimsOf(out.widgets[0]).rows).toBe(1);
  expect(out.widgets[1]).toMatchObject({ id: "a", y: 1 });

  // A row left empty on purpose between two cards stays.
  const gap = normalizeLayout({
    version: 4,
    widgets: [w("a", "apps", "4x1", "cards", 0, 0), w("b", "apps", "4x1", "cards", 0, 3)],
  });
  expect(gap.widgets.map((x) => x.y)).toEqual([0, 3]);

  // An edge-dragged build (8 x 5 units) at y 4, apps at y 9.
  const build = normalizeLayout({
    version: 4,
    widgets: [{ ...w("b", "build", "4x1", "bar", 0, 4), cols: 8, rows: 5 }, w("a", "apps", "4x1", "cards", 0, 9)],
  });
  expect(build.widgets.find((x) => x.id === "b")).toMatchObject({ y: 4 });
  expect(dimsOf(build.widgets.find((x) => x.id === "b")!).rows).toBe(4);
  expect(build.widgets.find((x) => x.id === "a")!.y).toBe(8);

  // A search with a v4 `rows: 2` is not rejected.
  const stored = normalizeLayout({ version: 4, widgets: [{ ...w("s", "search", "4x1", "bar", 0, 0), cols: 8, rows: 2 }] });
  expect(dimsOf(stored.widgets[0])).toEqual({ cols: 8, rows: 1 });
});

test("a v5 document is not collapsed", () => {
  const out = normalizeLayout({
    version: 5,
    widgets: [w("s", "search", "4x1", "bar", 0, 0), w("a", "apps", "4x1", "cards", 0, 2)],
  });
  expect(out.widgets.map((x) => x.y)).toEqual([0, 2]);
});

test("collapseShrunkRows leaves a vacated row another widget still covers", () => {
  const ws = [w("s", "search", "1x1", "bar", 0, 0), w("c", "bots", "1x1", "list", 2, 0), w("a", "apps", "4x1", "cards", 0, 2)] as any[];
  expect(collapseShrunkRows(ws, new Set([1])).map((x) => x.y)).toEqual([0, 0, 2]);
});

test("contentSizeFor picks the largest preset inside the footprint", () => {
  expect(contentSizeFor("apps", 6, 2)).toBe("2x1");
  expect(contentSizeFor("apps", 6, 4)).toBe("2x2");
  expect(contentSizeFor("tasks", 2, 2)).toBe(SOURCES.tasks.sizes[0]);
});

test("setSize on a custom widget clears the override even when the chip equals its content size", () => {
  const custom = solo({ cols: 3, rows: 2 });
  const l = setSize(custom, "a", "1x1");
  expect(l).not.toBe(custom);
  expect("cols" in only(l)).toBe(false);
  expect(only(l).size).toBe("1x1");
});

test("normalizeLayout keeps a valid v4 footprint, corrects size, and drops invalid ones", () => {
  const ok = normalizeLayout({ version: 4, widgets: [{ ...w("a", "bots", "2x2", "cards", 0, 0), cols: 3, rows: 2 }] });
  expect(only(ok).cols).toBe(3);
  expect(only(ok).rows).toBe(2);
  expect(only(ok).size).toBe("1x1");
  for (const bad of [{ cols: 9, rows: 2 }, { cols: 1, rows: 2 }, { cols: 3 }]) {
    const l = normalizeLayout({ version: 4, widgets: [{ ...w("a", "bots", "1x1", "cards", 0, 0), ...bad }] });
    expect("cols" in only(l)).toBe(false);
    expect("rows" in only(l)).toBe(false);
  }
});

test("rectOf / rowsUsed / canPlace / occupancy honour the override", () => {
  const l = solo({ cols: 3, rows: 4 });
  expect(rectOf(only(l))).toEqual({ x: 0, y: 0, cols: 3, rows: 4 });
  expect(rowsUsed(l.widgets)).toBe(4);
  expect(occupancy(l.widgets).get("2,0")).toBe("a");
  expect(canPlace(l.widgets, { x: 2, y: 0, cols: 2, rows: 2 })).toBe(false);
  expect(canPlace(l.widgets, { x: 3, y: 0, cols: 2, rows: 2 })).toBe(true);
});

// ---- Presets and tile swap ---------------------------------------------------

const IDS = PRESETS.map((p) => p.id);

test("presets: no overlaps, in bounds, no holes", () => {
  for (const id of IDS) {
    for (const folderId of [undefined, "f1"]) {
      const l = presetLayout(id, { folderId });
      expect(l.version).toBe(5);
      const rows = rowsUsed(l.widgets);
      const seen = new Set<string>();
      for (const w of l.widgets) {
        const r = rectOf(w);
        expect(r.x >= 0 && r.x + r.cols <= GRID_COLS).toBe(true);
        for (let j = 0; j < r.rows; j++) {
          for (let i = 0; i < r.cols; i++) {
            const k = `${r.x + i},${r.y + j}`;
            expect(seen.has(k)).toBe(false);
            seen.add(k);
          }
        }
      }
      expect(seen.size).toBe(rows * GRID_COLS);
      expect(emptySlots(l)).toEqual([]);
    }
  }
});

test("presets: normalizeLayout keeps every tile where it is", () => {
  for (const id of IDS) {
    const l = presetLayout(id, { folderId: "f1" });
    const n = normalizeLayout(l);
    expect(n.widgets.map((w) => [w.source, w.x, w.y, dimsOf(w)])).toEqual(
      l.widgets.map((w) => [w.source, w.x, w.y, dimsOf(w)]),
    );
  }
  const files = presetLayout("files");
  expect(normalizeLayout(files).widgets.length).toBe(files.widgets.length);
});

test("presets: legacy is first; builder is the default; workbench is gone; files falls back without a folder", () => {
  const strip = (l: HomeLayout) => l.widgets.map(({ id: _i, ...w }) => w);
  expect(PRESETS[0].id).toBe("legacy");
  expect(strip(presetLayout("builder"))).toEqual(strip(defaultLayout()));
  expect(presetLayout("builder").widgets[0].id).not.toBe("default-search");
  expect(PRESETS.some((p) => (p.id as string) === "workbench")).toBe(false);
  expect(presetLayout("legacy").widgets.map((x) => [x.source, x.x, x.y])).toEqual([
    ["search", 0, 0],
    ["apps", 0, 1],
    ["sessions", 0, 5],
    ["recents", 0, 9],
  ]);
  const mission = presetLayout("mission").widgets;
  expect(mission.map((x) => [x.source, x.x, x.y])).toEqual([
    ["search", 0, 0],
    ["tasks", 0, 1],
    ["bots", 0, 7],
    ["sessions", 4, 7],
  ]);
  const mt = mission.find((x) => x.source === "tasks")!;
  expect(mt.show).toBe("open");
  expect(dimsOf(mt)).toEqual({ cols: 8, rows: 6 });
  expect(presetLayout("files").widgets.some((w) => w.source === "folder")).toBe(false);
  const f = presetLayout("files", { folderId: "f1" }).widgets.find((w) => w.source === "folder");
  expect(f?.folderId).toBe("f1");
});

test("matchPreset: each preset matches itself, custom is null", () => {
  for (const id of IDS) {
    expect(matchPreset(presetLayout(id, { folderId: "f1" }))).toBe(id);
    expect(matchPreset(presetLayout(id))).toBe(id);
  }
  expect(matchPreset(defaultLayout())).toBe("builder");
  const l = presetLayout("mission");
  const tasks = l.widgets.find((w) => w.source === "tasks")!;
  expect(matchPreset(swapSource(l, tasks.id, "playground"))).toBe(null);
  expect(matchPreset({ ...l, widgets: [] })).toBe(null);
});

test("sourceFits: search", () => {
  const l = presetLayout("focus");
  const search = l.widgets[0];
  expect(sourceFits(l, { x: 0, y: 0, cols: 4, rows: 1 }, "search", search.id)).toEqual({ ok: true });
  expect(sourceFits(l, { x: 0, y: 2, cols: 4, rows: 1 }, "search")).toEqual({ ok: false, reason: "Already on Home" });
  const none = removeWidget(l, search.id);
  expect(sourceFits(none, { x: 0, y: 0, cols: 4, rows: 2 }, "search")).toEqual({ ok: false, reason: "Needs a one-row strip" });
  expect(sourceFits(none, { x: 0, y: 0, cols: 1, rows: 1 }, "search")).toEqual({ ok: false, reason: "Needs a one-row strip" });
  expect(sourceFits(none, { x: 0, y: 0, cols: 2, rows: 1 }, "search")).toEqual({ ok: true });
});

test("sourceFits: build needs a 4-row, 4-col tile", () => {
  const l = { version: 5 as const, widgets: [] };
  expect(sourceFits(l, { x: 0, y: 0, cols: 4, rows: FIXED_ROWS.build! }, "build")).toEqual({ ok: true });
  expect(sourceFits(l, { x: 0, y: 0, cols: 6, rows: 4 }, "build")).toEqual({ ok: true });
  expect(sourceFits(l, { x: 0, y: 0, cols: 2, rows: 4 }, "build")).toEqual({ ok: false, reason: "Needs a bigger tile" });
  expect(sourceFits(l, { x: 0, y: 0, cols: 4, rows: 2 }, "build")).toEqual({ ok: false, reason: "Needs a bigger tile" });
});

test("sourceFits: other sources", () => {
  const l = { version: 5 as const, widgets: [] };
  expect(sourceFits(l, { x: 0, y: 0, cols: 4, rows: 1 }, "apps")).toEqual({ ok: false, reason: "Needs a taller tile" });
  expect(sourceFits(l, { x: 0, y: 0, cols: 4, rows: 4 }, "tasks")).toEqual({ ok: true });
  expect(sourceFits(l, { x: 0, y: 0, cols: 4, rows: 2 }, "apps")).toEqual({ ok: true });
  expect(sourceFits(l, { x: 0, y: 0, cols: 4, rows: 2 }, "app")).toEqual({ ok: true });
  expect(sourceFits(l, { x: 0, y: 0, cols: 2, rows: 4 }, "recents")).toEqual({ ok: true });
  expect(sourceFits(l, { x: 0, y: 0, cols: 2, rows: MAX_WIDGET_ROWS + 1 }, "apps")).toEqual({ ok: false, reason: "Too tall" });
});

test("swapSource keeps id and rectangle", () => {
  const l = presetLayout("files");
  const tasks = l.widgets.find((w) => w.source === "tasks")!;
  const next = swapSource(l, tasks.id, "recents");
  const w = next.widgets.find((x) => x.id === tasks.id)!;
  expect(w.source).toBe("recents");
  expect(rectOf(w)).toEqual(rectOf(tasks));
  expect(w.format).toBe("list");
  expect(next.widgets.length).toBe(l.widgets.length);
  expect(normalizeLayout(next).widgets.find((x) => x.id === tasks.id)).toEqual(w);
  const idx = l.widgets.find((x) => x.source === "index")!;
  const small = swapSource(l, idx.id, "bots");
  expect(small.widgets.find((x) => x.id === idx.id)).toMatchObject({ source: "bots", size: "1x1" });
  expect(small.widgets.find((x) => x.id === idx.id)?.cols).toBeUndefined();
});

test("swapSource: a footprint no preset matches is stored explicitly", () => {
  const l = presetLayout("files");
  const rec = l.widgets.find((w) => w.source === "recents")!;
  const w = swapSource(l, rec.id, "apps").widgets.find((x) => x.id === rec.id)!;
  expect(w).toMatchObject({ source: "apps", cols: 6, rows: 4 });
});

test("swapSource: no-op cases return the same object", () => {
  const l = presetLayout("files");
  const bots = l.widgets.find((w) => w.source === "bots")!;
  const search = l.widgets.find((w) => w.source === "search")!;
  expect(swapSource(l, bots.id, "build")).toBe(l);
  expect(swapSource(l, bots.id, "search")).toBe(l);
  expect(swapSource(l, bots.id, "bots")).toBe(l);
  expect(swapSource(l, bots.id, "folder")).toBe(l);
  expect(swapSource(l, search.id, "apps")).toBe(l);
  expect(swapSource(l, "nope", "apps")).toBe(l);
});

test("swapSource: folder and app carry their fields, leaving drops them", () => {
  const l = presetLayout("files");
  const bots = l.widgets.find((w) => w.source === "tasks")!;
  const f = swapSource(l, bots.id, "folder", { folderId: "f1", format: "icons" });
  expect(f.widgets.find((x) => x.id === bots.id)).toMatchObject({ source: "folder", folderId: "f1", format: "icons" });
  const a = swapSource(f, bots.id, "app", { appPath: "/x/app" });
  const aw = a.widgets.find((x) => x.id === bots.id)!;
  expect(aw).toMatchObject({ source: "app", appPath: "/x/app", format: "live" });
  expect(aw.folderId).toBeUndefined();
  const back = swapSource(a, bots.id, "index").widgets.find((x) => x.id === bots.id)!;
  expect(back.appPath).toBeUndefined();
  expect(back.folderId).toBeUndefined();
  const apps = swapSource(l, bots.id, "apps", { sort: "name" }).widgets.find((x) => x.id === bots.id)!;
  expect(apps.sort).toBe("name");
});

test("emptySlots: preset layouts have none, a removed tile leaves its rectangle", () => {
  expect(emptySlots({ version: 5, widgets: [] })).toEqual([]);
  const l = presetLayout("files");
  const tasks = l.widgets.find((w) => w.source === "tasks")!;
  expect(emptySlots(removeWidget(l, tasks.id))).toEqual([rectOf(tasks)]);
  const idx = l.widgets.find((w) => w.source === "index")!;
  expect(emptySlots(removeWidget(l, idx.id))).toEqual([rectOf(idx)]);
  const search = l.widgets.find((w) => w.source === "search")!;
  expect(emptySlots(removeWidget(l, search.id))).toEqual([]);
});

test("fillSlot: adds exactly the rectangle; refuses what does not fit", () => {
  const l = presetLayout("files");
  const tasks = l.widgets.find((w) => w.source === "tasks")!;
  const hole = removeWidget(l, tasks.id);
  const [slot] = emptySlots(hole);
  const next = fillSlot(hole, slot, "apps");
  expect(next.widgets.length).toBe(l.widgets.length);
  const w = next.widgets.find((x) => !l.widgets.some((o) => o.id === x.id))!;
  expect(w.source).toBe("apps");
  expect(rectOf(w)).toEqual(slot);
  expect(emptySlots(next)).toEqual([]);
  expect(fillSlot(hole, slot, "build")).not.toBe(hole);
  const idx = l.widgets.find((w) => w.source === "index")!;
  const small = removeWidget(l, idx.id);
  expect(fillSlot(small, rectOf(idx), "build")).toBe(small);
  expect(fillSlot(hole, slot, "search")).toBe(hole);
  expect(fillSlot(hole, slot, "folder")).toBe(hole);
  expect(fillSlot(l, slot, "apps")).toBe(l);
  const full = { ...hole, widgets: [...hole.widgets, ...Array.from({ length: 48 }, (_, i) => ({ ...hole.widgets[1], id: `z${i}`, x: 0, y: 100 + i }))].slice(0, 48) };
  expect(fillSlot(full, slot, "apps")).toBe(full);
});

test("sourceFits: a source needs a size that fits the rect outright", () => {
  const l = { version: 5 as const, widgets: [] };
  const r = { x: 0, y: 0, cols: 2, rows: 2 };
  expect(sourceFits(l, r, "tasks")).toEqual({ ok: false, reason: "Needs a bigger tile" });
  expect(sourceFits(l, r, "bots")).toEqual({ ok: true });
  expect(sourceFits(l, r, "index")).toEqual({ ok: true });
});

test("sourceFits: every preset widget fits its own rect", () => {
  for (const { id } of PRESETS) {
    for (const folderId of ["f1", undefined]) {
      const l = presetLayout(id, { folderId });
      for (const w of l.widgets) expect(sourceFits(l, rectOf(w), w.source, w.id)).toEqual({ ok: true });
    }
  }
});

test("emptySlots: a gap taller than a tile is split into stacked slots", () => {
  const base = defaultLayout().widgets[0];
  const l: HomeLayout = {
    ...defaultLayout(),
    widgets: [
      { ...base, id: "a", x: 0, y: 0, cols: 8, rows: 1 },
      { ...base, id: "b", x: 0, y: 11, cols: 8, rows: 1 },
    ],
  };
  const slots = emptySlots(l);
  expect(slots.length).toBeGreaterThan(1);
  expect(slots.every((s) => s.rows <= MAX_WIDGET_ROWS && s.x === 0 && s.cols === 8)).toBe(true);
  expect(slots.reduce((n, s) => n + s.rows, 0)).toBe(10);
  expect(Math.min(...slots.map((s) => s.y))).toBe(1);
});

test("defaultFormat: card strips narrower than a full row use their compact format", () => {
  expect(defaultFormat("sessions", 4)).toBe("list");
  expect(defaultFormat("sessions", 8)).toBe("cards");
  expect(defaultFormat("apps", 4)).toBe("icons");
  expect(defaultFormat("tasks", 4)).toBe("list");
});

test("swapSource into a 4-unit tile opens card strips as a list", () => {
  const l = presetLayout("files");
  const rec = l.widgets.find((w) => w.source === "tasks")!;
  expect(rectOf(rec).cols).toBe(4);
  const w = swapSource(l, rec.id, "sessions").widgets.find((x) => x.id === rec.id)!;
  expect(w.source).toBe("sessions");
  expect(w.format).toBe("list");
});

test("preset card-strip tiles narrower than half a row are not cards", () => {
  for (const p of PRESETS) {
    for (const w of presetLayout(p.id).widgets) {
      if (!["apps", "playground", "sessions", "recents"].includes(w.source)) continue;
      if (rectOf(w).cols < GRID_COLS / 2) expect(w.format).not.toBe("cards");
    }
  }
});

test("Mission control lists Bots and Claude Sessions; Files shows recents as cards", () => {
  const m = presetLayout("mission").widgets;
  expect(m.find((w) => w.source === "bots")!.format).toBe("list");
  expect(m.find((w) => w.source === "sessions")!.format).toBe("list");
  const f = presetLayout("files").widgets;
  expect(f.find((w) => w.source === "recents")!.format).toBe("cards");
});

test("Builder preset shows Bots at 0,5 and no recent files", () => {
  const ws = presetLayout("builder").widgets;
  expect(ws.some((w) => w.source === "recents")).toBe(false);
  const bots = ws.find((w) => w.source === "bots")!;
  expect([bots.x, bots.y]).toEqual([0, 5]);
});

test("no preset has a playground tile", () => {
  for (const p of PRESETS) {
    expect(presetLayout(p.id).widgets.some((w) => w.source === "playground")).toBe(false);
  }
});

test("FORMAT_MIN_ROWS: icons need two cells (4 units)", () => {
  expect(FORMAT_MIN_ROWS.icons).toBe(4);
});

test("sizesFor drops presets too short for the format, fixed-row sources are unaffected", () => {
  expect(sizesFor("apps", "icons")).toEqual(["1x2", "2x2"]);
  expect(sizesFor("apps", "cards")).toEqual(SOURCES.apps.sizes);
  expect(sizesFor("apps")).toEqual(SOURCES.apps.sizes);
  expect(sizesFor("folder", "icons")).toEqual(["1x2", "2x2"]);
  expect(sizesFor("search", "bar")).toEqual(SOURCES.search.sizes);
  expect(sizesFor("build", "live")).toEqual(SOURCES.build.sizes);
});

test("minFootprint with a format is at least the format's rows", () => {
  expect(minFootprint("apps").rows).toBe(2);
  expect(minFootprint("apps", "icons").rows).toBe(4);
  expect(minFootprint("apps", "cards").rows).toBe(2);
  expect(minFootprint("search", "bar").rows).toBe(1);
});

test("formatForRows keeps a format that fits and falls back otherwise", () => {
  expect(formatForRows("apps", "icons", 4)).toBe("icons");
  expect(formatForRows("apps", "icons", 2)).toBe("cards");
  expect(formatForRows("folder", "icons", 2)).toBe("list");
});

test("allowedSizes for an icons tile never offers a short size", () => {
  const l = lay(w("a", "apps", "2x2", "icons"));
  expect(allowedSizes(l, "a")).toEqual(["1x2", "2x2"]);
});

test("normalizeLayout: a stored icons tile that is too short gets the source's first fitting format, unmoved", () => {
  const out = normalizeLayout({ version: 5, widgets: [{ id: "a", source: "apps", size: "2x1", format: "icons", x: 2, y: 1 }] });
  expect(out.widgets[0]).toMatchObject({ format: "cards", size: "2x1", x: 2, y: 1 });
  const tall = normalizeLayout({ version: 5, widgets: [{ id: "a", source: "apps", size: "2x2", format: "icons", x: 0, y: 0 }] });
  expect(tall.widgets[0].format).toBe("icons");
});

test("setFormat refuses icons on a short tile", () => {
  const l = lay({ ...w("a", "apps", "2x1", "cards"), x: 0, y: 0 });
  expect(setFormat(l, "a", "icons")).toBe(l);
});

test("setSize refuses a size too short for the tile's format", () => {
  const l = lay({ ...w("a", "apps", "2x2", "icons"), x: 0, y: 0 });
  expect(setSize(l, "a", "2x1")).toBe(l);
  expect(setSize(l, "a", "1x2").widgets[0].size).toBe("1x2");
});

test("normalizeLayout: a stored icons tile one cell tall keeps its footprint and drops to another format", () => {
  const raw = {
    version: 5,
    widgets: [{ id: "a", source: "apps", size: "2x1", format: "icons", x: 2, y: 3, cols: 5, rows: 2 }],
  };
  const out = normalizeLayout(raw).widgets[0];
  expect(out).toMatchObject({ x: 2, y: 3, format: "cards" });
  expect(dimsOf(out)).toEqual({ cols: 5, rows: 2 });
});
