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

const w = (id: string, source: any = "apps", size: any = "4x1", format: any = "cards") => ({
  id,
  source,
  size,
  format,
});
const lay = (...widgets: any[]): HomeLayout => ({ version: 1, widgets });
const ids = (l: HomeLayout) => l.widgets.map((x) => x.id);

test("default layout reproduces today's four strips in order", () => {
  expect(DEFAULT_LAYOUT.widgets.map((x) => [x.source, x.size, x.format])).toEqual([
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
});

test("normalizeLayout falls back to default on garbage", () => {
  expect(normalizeLayout(null)).toEqual(DEFAULT_LAYOUT);
  expect(normalizeLayout({ version: 2, widgets: [] })).toEqual(DEFAULT_LAYOUT);
  expect(normalizeLayout({ version: 1, widgets: "x" })).toEqual(DEFAULT_LAYOUT);
});

test("normalizeLayout keeps an intentionally empty layout", () => {
  expect(normalizeLayout({ version: 1, widgets: [] }).widgets).toEqual([]);
});

test("normalizeLayout drops unknown sources and folder widgets with no folderId", () => {
  const out = normalizeLayout({
    version: 1,
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
    version: 1,
    widgets: [w("a", "index", "4x1", "cards"), w("b", "tasks", "1x1", "icons")],
  });
  expect(out.widgets[0]).toMatchObject({ size: "1x1", format: "count" });
  expect(out.widgets[1]).toMatchObject({ size: "2x2", format: "list" });
});

test("normalizeLayout gives duplicate/missing ids fresh unique ids", () => {
  const out = normalizeLayout({ version: 1, widgets: [w("a"), w("a"), { ...w("x"), id: undefined }] });
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
});

test("removeWidget / setSize / setFormat", () => {
  const l = lay(w("a"), w("b", "tasks", "2x2", "list"));
  expect(ids(removeWidget(l, "a"))).toEqual(["b"]);
  expect(setSize(l, "b", "4x1").widgets[1].size).toBe("4x1");
  expect(setSize(l, "b", "1x1")).toBe(l);
  expect(setFormat(l, "b", "board").widgets[1].format).toBe("board");
  expect(setFormat(l, "b", "cards")).toBe(l);
  expect(setFormat(l, "zzz", "board")).toBe(l);
});
