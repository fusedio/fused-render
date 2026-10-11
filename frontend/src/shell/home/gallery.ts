// Pure helpers behind the add sheet's widget gallery: the list of variants
// (one per source and format), the pixel math for their scaled previews, and
// the "how much room is left" note.
import {
  CELL,
  FORMAT_LABELS,
  GRID_COLS,
  SIZE_LABELS,
  MAX_WIDGETS,
  SOURCES,
  dimsFor,
  dimsOf,
  firstFreeRect,
  FIXED_ROWS,
  canPlace,
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
  /** The format label alone when the source has several formats. */
  formatLabel?: string;
  /** "2×2" in whole cells; a fixed-row source reads its width: "Full row", "Half" or "Small". */
  footprint: string;
  description: string;
  /** Why the entry cannot be picked; absent when it can. */
  disabled?: string;
}

/** Largest scale a preview is drawn at. */
export const MAX_PREVIEW_SCALE = 0.6;

/** Pixels spanned by `n` units of `unit` pixels plus the 16px gaps between them. */
export function previewPx(n: number, unit: number): number {
  return n * unit + (n - 1) * 16;
}

/** The one scale every preview shares: a full-row preview fits `avail` pixels. */
export function previewScale(fullRowPx: number, avail: number): number {
  return Math.min(MAX_PREVIEW_SCALE, avail / fullRowPx);
}

/** "2×2" in whole cells, with a half for a one-unit height ("4×½"). */
export function footprintLabel(cols: number, rows: number): string {
  const f = (n: number) => (n % CELL === 0 ? String(n / CELL) : n === 1 ? "½" : `${Math.floor(n / CELL)}½`);
  return `${f(cols)}×${f(rows)}`;
}

/** A fixed-row source has one height, so its width names the size. */
function fixedRowLabel(cols: number, rows: number): string {
  if (cols === GRID_COLS) return "Full row";
  if (cols === GRID_COLS / 2) return SIZE_LABELS["2x1"];
  if (cols === CELL) return SIZE_LABELS["1x1"];
  return footprintLabel(cols, rows);
}

/** Every variant the sheet offers, in SOURCES order. With a `target` (opened
    from a tile's Change card) only `only` is listed, and entries that do not fit
    the tile are disabled. */
export function galleryEntries(
  layout: HomeLayout,
  target?: TileTarget,
  only?: WidgetSource,
  at?: { x: number; y: number },
): GalleryEntry[] {
  const out: GalleryEntry[] = [];
  const full = (!target || target.kind === "fill") && layout.widgets.length >= MAX_WIDGETS;
  const tile = target ? (target.kind === "swap" ? { x: 0, y: 0, ...dimsOf(target.widget) } : target.rect) : null;
  const replacing = target?.kind === "swap" ? target.widget.id : undefined;
  for (const source of Object.keys(SOURCES) as WidgetSource[]) {
    if (only && source !== only) continue;
    const spec = SOURCES[source];
    for (const format of spec.formats) {
      const sizes = sizesFor(source, format);
      if (sizes[0] === undefined) continue;
      // A swap keeps the tile's footprint: use the first size that fits it.
      // Aimed at a grid spot (no target): the default size if it fits there, else the first that does.
      const size =
        (tile &&
          sizes.find((s) => {
            const d = dimsFor(source, s);
            return d.cols <= tile.cols && d.rows <= tile.rows;
          })) ||
        (!target &&
          at &&
          sizes.find((s) => {
            const d = dimsFor(source, s);
            return at.x + d.cols <= GRID_COLS && canPlace(layout.widgets, { x: at.x, y: at.y, ...d });
          })) ||
        sizes[0];
      const preset = dimsFor(source, size);
      // A swap or fill covers exactly the tile, so its preview shows the tile.
      const { cols, rows } = tile ? { cols: tile.cols, rows: tile.rows } : preset;
      let disabled: string | undefined;
      if (full) disabled = "Home is full";
      else if (tile) {
        if (preset.cols > tile.cols || preset.rows > tile.rows) disabled = "Too big for this tile";
        else {
          const fit = sourceFits(layout, { ...tile, x: 0, y: 0 }, source, replacing);
          if (!fit.ok) disabled = fit.reason;
        }
      } else if (source === "search" && hasSearch(layout)) disabled = "Already on Home";
      else if (at && (at.x + cols > GRID_COLS || !canPlace(layout.widgets, { x: at.x, y: at.y, cols, rows })))
        disabled = "No room here";
      out.push({
        key: `${source}:${format}`,
        source,
        format,
        size,
        cols,
        rows,
        title: spec.formats.length > 1 ? `${spec.label} · ${FORMAT_LABELS[format]}` : spec.label,
        formatLabel: spec.formats.length > 1 ? FORMAT_LABELS[format] : undefined,
        footprint: FIXED_ROWS[source] !== undefined ? fixedRowLabel(cols, rows) : footprintLabel(cols, rows),
        description: spec.description,
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
