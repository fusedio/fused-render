// Miniature 4x2 grid with the widget's footprint filled, used by the size menu.
import { CELL, dims, FIXED_ROWS, type WidgetSize, type WidgetSource } from "./layout";

const COLS = 4;
const ROWS = 2;

/** Row-major flags for the 4x2 grid: true where the widget covers the cell. */
export function footprintCells(size: WidgetSize): boolean[] {
  const d = dims(size);
  // dims() is in half-cell units; the glyph draws whole cells.
  const cols = d.cols / CELL;
  const rows = d.rows / CELL;
  const cells: boolean[] = [];
  for (let r = 0; r < ROWS; r++) for (let c = 0; c < COLS; c++) cells.push(c < cols && r < rows);
  return cells;
}

export function SizeGlyph({ size, scale = 1, source }: { size: WidgetSize; scale?: number; source?: WidgetSource }) {
  const cells = footprintCells(size);
  // A fixed-row source is one unit tall: the same widths, drawn as a flat bar.
  const flat = source !== undefined && FIXED_ROWS[source] !== undefined;
  return (
    <svg aria-hidden="true" viewBox="0 0 42 20" width={42 * scale} height={20 * scale}>
      {cells.map((on, i) => {
        const x = (i % COLS) * 11;
        const y = Math.floor(i / COLS) * 11;
        if (flat) {
          // Only the first row carries the bar; it is half a cell tall.
          if (i >= COLS) return null;
          return on ? (
            <rect key={i} x={x} y={y} width="9" height="4" rx="1.5" fill="currentColor" />
          ) : (
            <rect key={i} x={x + 0.5} y={y + 0.5} width="8" height="3" rx="1" fill="none" stroke="currentColor" strokeWidth="1" opacity="0.35" />
          );
        }
        return on ? (
          <rect key={i} x={x} y={y} width="9" height="9" rx="2" fill="currentColor" />
        ) : (
          <rect key={i} x={x + 0.5} y={y + 0.5} width="8" height="8" rx="1.5" fill="none" stroke="currentColor" strokeWidth="1" opacity="0.35" />
        );
      })}
    </svg>
  );
}
