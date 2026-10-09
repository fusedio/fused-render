import { expect, test } from "bun:test";
import { CELL, GRID_COLS, MAX_WIDGETS, SOURCES, addWidget, defaultLayout, sizesFor, type HomeLayout, type Widget, type WidgetSource } from "./layout";
import { footprintLabel, freeSpaceNote, galleryEntries, previewPx, previewScale } from "./gallery";

const empty: HomeLayout = { ...defaultLayout(), widgets: [] };
const w = (id: string, source: Widget["source"], x: number, y: number, cols: number, rows: number): Widget => ({
  id,
  source,
  size: SOURCES[source].sizes[0],
  format: SOURCES[source].formats[0],
  x,
  y,
  cols,
  rows,
});

test("one entry per (source, format) at the format's default size", () => {
  const es = galleryEntries(empty);
  for (const e of es) expect(e.size).toBe(sizesFor(e.source, e.format)[0]);
  const apps = es.filter((e) => e.source === "apps");
  expect(apps.map((e) => e.format)).toEqual(["cards", "icons"]);
  // Icons need two cells of height, so the default size is the first that fits.
  expect(apps[1].rows).toBeGreaterThanOrEqual(2 * CELL);
});

test("entries follow SOURCES order and sections group by source", () => {
  const es = galleryEntries(empty);
  const order = [...new Set(es.map((e) => e.source))];
  expect(order).toEqual((Object.keys(SOURCES) as WidgetSource[]).filter((s) => order.includes(s)));
});

test("fixed-row sources have a single entry; titles name the format only when several", () => {
  const es = galleryEntries(empty);
  expect(es.filter((e) => e.source === "search")).toHaveLength(1);
  expect(es.filter((e) => e.source === "build")).toHaveLength(1);
  expect(es.find((e) => e.source === "search")!.title).toBe("File search");
  expect(es.filter((e) => e.source === "tasks").map((e) => e.title)).toEqual(["Tasks · List", "Tasks · Board", "Tasks · Count"]);
  expect(es.find((e) => e.source === "index")!.title).toBe("File index");
  expect(es.find((e) => e.source === "apps")!.description).toBe(SOURCES.apps.description);
});

test("a second search stays listed but disabled with the note", () => {
  const layout = addWidget(empty, "search");
  const e = galleryEntries(layout).find((x) => x.source === "search")!;
  expect(e.disabled).toBe("Already on Home");
  expect(e.description).toBe(SOURCES.search.description);
});

test("a full Home disables every entry", () => {
  const widgets = Array.from({ length: MAX_WIDGETS }, (_, i) => w("w" + i, "index", 0, i * 2, 2, 2));
  const es = galleryEntries({ ...empty, widgets });
  expect(es.every((e) => e.disabled === "Home is full")).toBe(true);
});

test("target mode disables entries bigger than the tile and keeps ones that fit", () => {
  const tile = w("t", "folder", 0, 0, 2 * CELL, 1 * CELL);
  const layout = { ...empty, widgets: [tile] };
  const es = galleryEntries(layout, { kind: "swap", widget: tile }, "folder");
  expect(es.every((e) => e.source === "folder")).toBe(true);
  for (const e of es) {
    const fits = e.cols <= 2 * CELL && e.rows <= CELL;
    expect(e.disabled).toBe(fits ? undefined : "Too big for this tile");
  }
  expect(es.some((e) => e.disabled === undefined)).toBe(true);
});

test("target mode on an empty slot checks the slot rect", () => {
  const layout = empty;
  const es = galleryEntries(layout, { kind: "fill", rect: { x: 0, y: 0, cols: 4, rows: 4 } }, "app");
  expect(es.every((e) => e.source === "app")).toBe(true);
  expect(es.find((e) => e.format === "live")!.size).toBe("2x2");
  expect(es[0].disabled).toBeUndefined();
});

test("footprint label in whole cells, halves for a one-unit row", () => {
  expect(footprintLabel(4, 4)).toBe("2×2");
  expect(footprintLabel(8, 4)).toBe("4×2");
  expect(footprintLabel(8, 1)).toBe("4×½");
  expect(footprintLabel(2, 2)).toBe("1×1");
});

test("preview pixels span n units of the given size plus the 16px gaps", () => {
  expect(previewPx(1, 52)).toBe(52);
  expect(previewPx(2, 52)).toBe(120);
  expect(previewPx(1, 100)).toBe(100);
  expect(previewPx(GRID_COLS, 100)).toBe(8 * 100 + 7 * 16);
});

test("one scale fits a full-row preview into the width, capped at 0.6", () => {
  const full = previewPx(GRID_COLS, 100);
  expect(previewScale(full, 2000)).toBe(0.6);
  expect(previewScale(full, full / 2)).toBeCloseTo(0.5);
});

test("fixed-row sources read Full row, never a fraction", () => {
  const es = galleryEntries(empty);
  expect(es.find((e) => e.source === "search")!.footprint).toBe("Full row");
  expect(es.find((e) => e.source === "build")!.footprint).toBe("Full row");
  expect(es.find((e) => e.source === "index")!.footprint).toBe(footprintLabel(es.find((e) => e.source === "index")!.cols, es.find((e) => e.source === "index")!.rows));
  expect(es.every((e) => !e.footprint.includes("½"))).toBe(true);
});

test("free space note: cells left inside the used rows, else a new row", () => {
  expect(freeSpaceNote([])).toBe("Adds a new row");
  expect(freeSpaceNote([w("a", "index", 0, 0, GRID_COLS, 4)])).toBe("Adds a new row");
  // Used rows are 4 units tall: 8 free units (2 columns) make 2 cells; 4 free units make 1.
  expect(freeSpaceNote([w("a", "index", 0, 0, 4, 4), w("b", "index", 4, 0, 2, 4)])).toBe("2 free cells");
  expect(freeSpaceNote([w("a", "index", 0, 0, 6, 4), w("b", "index", 6, 0, 2, 2)])).toBe("1 free cell");
  // A one-unit sliver cannot hold a cell.
  expect(freeSpaceNote([w("a", "index", 0, 0, 7, 4)])).toBe("Adds a new row");
});
