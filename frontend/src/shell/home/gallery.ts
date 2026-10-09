// Pure helpers behind the add sheet's widget gallery: the list of variants
// (one per source and format), the pixel math for their scaled previews, and
// the "how much room is left" note.
import {
  CELL,
  FORMAT_LABELS,
  GRID_COLS,
  MAX_WIDGETS,
  SOURCES,
  dimsFor,
  dimsOf,
  firstFreeRect,
  hasSearch,
  occupancy,
  rectOf,
  rowsUsed,
  sizesFor,
  sourceFits,
  type HomeLayout,
  type TileTarget,
  type Widget,
  type WidgetFormat,
  type WidgetSize,
  type WidgetSource,
} from "./layout";

/** One gallery card: a source drawn in one format at that format's default size. */
export interface GalleryEntry {
  key: string;
  source: WidgetSource;
  format: WidgetFormat;
  size: WidgetSize;
  /** Footprint in grid units. */
  cols: number;
  rows: number;
  /** Source label, plus the format when the source has several. */
  title: string;
  description: string;
  /** Why the entry cannot be picked; absent when it can. */
  disabled?: string;
}

/** Scale the previews are drawn at. */
export const PREVIEW_SCALE = 0.5;

/** Pixels spanned by `n` grid units: the 52px unit plus the 16px gaps between them. */
export function previewPx(n: number): number {
  return n * 52 + (n - 1) * 16;
}

/** "2×2" in whole cells, with a half for a one-unit height ("4×½"). */
export function footprintLabel(cols: number, rows: number): string {
  const f = (n: number) => (n % CELL === 0 ? String(n / CELL) : n === 1 ? "½" : `${Math.floor(n / CELL)}½`);
  return `${f(cols)}×${f(rows)}`;
}

/** Every variant the sheet offers, in SOURCES order. With a `target` (opened
    from a tile's Change card) only `only` is listed, and entries that do not fit
    the tile are disabled. */
export function galleryEntries(layout: HomeLayout, target?: TileTarget, only?: WidgetSource): GalleryEntry[] {
  const out: GalleryEntry[] = [];
  const full = (!target || target.kind === "fill") && layout.widgets.length >= MAX_WIDGETS;
  const tile = target ? (target.kind === "swap" ? { x: 0, y: 0, ...dimsOf(target.widget) } : target.rect) : null;
  const replacing = target?.kind === "swap" ? target.widget.id : undefined;
  for (const source of Object.keys(SOURCES) as WidgetSource[]) {
    if (only && source !== only) continue;
    const spec = SOURCES[source];
    for (const format of spec.formats) {
      const size = sizesFor(source, format)[0];
      if (size === undefined) continue;
      const { cols, rows } = dimsFor(source, size);
      let disabled: string | undefined;
      if (full) disabled = "Home is full";
      else if (tile) {
        if (cols > tile.cols || rows > tile.rows) disabled = "Too big for this tile";
        else {
          const fit = sourceFits(layout, { ...tile, x: 0, y: 0 }, source, replacing);
          if (!fit.ok) disabled = fit.reason;
        }
      } else if (source === "search" && hasSearch(layout)) disabled = "Already on Home";
      out.push({
        key: `${source}:${format}`,
        source,
        format,
        size,
        cols,
        rows,
        title: spec.formats.length > 1 ? `${spec.label} · ${FORMAT_LABELS[format]}` : spec.label,
        description: disabled === "Already on Home" ? disabled : spec.description,
        disabled,
      });
    }
  }
  return out;
}

/** What the next widget will take: "N free cells" while a one-cell tile still
    fits inside the rows already used, else "Adds a new row". */
export function freeSpaceNote(widgets: Widget[]): string {
  const used = rowsUsed(widgets);
  if (firstFreeRect(widgets.map(rectOf), CELL, CELL).y >= used) return "Adds a new row";
  const free = used * GRID_COLS - occupancy(widgets).size;
  const n = Math.floor(free / (CELL * CELL));
  return n === 1 ? "1 free cell" : `${n} free cells`;
}
