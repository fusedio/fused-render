// The widget grid. Every widget owns explicit half-cell units (x, y) on an 8-unit grid.
// Edit mode does not move or resize anything: each tile can Change what it
// shows (Widget.tsx), every hole the layout has shows a "Choose what goes
// here" slot, and the trailing "+ Add widget" tile stays. Under NARROW_MAX the
// grid shows a 4-unit reflow of the same coordinates (derived, never stored)
// without slots.
import { useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { WidgetFrame } from "./Widget";
import { ChangeTile } from "./ChangeTile";
import type { HomeLayoutApi } from "./useHomeLayout";
import {
  CELL,
  GRID_COLS,
  dimsOf,
  emptySlots,
  reflowToColumns,
  rectOf,
  type Rect,
  type TileTarget,
  type WidgetSource,
} from "./layout";

/** First widget of each source carries the tour anchor the strips used to. */
const ANCHOR_SOURCES = new Set(["search", "apps", "playground", "sessions"]);

/** At or below this grid width the layout reflows to 2 columns. */
const NARROW_MAX = 640;

function placement(r: Rect): CSSProperties {
  return { gridColumn: `${r.x + 1} / span ${r.cols}`, gridRow: `${r.y + 1} / span ${r.rows}` };
}

export function WidgetGrid({
  api,
  edit,
  searching = false,
  onAdd,
  onRequestPanel,
}: {
  api: HomeLayoutApi;
  edit: boolean;
  /** A search query is live: only the search widget renders, full width. */
  searching?: boolean;
  onAdd: () => void;
  /** A folder or page pick from a Change popover: open the add sheet aimed at that tile or slot. */
  onRequestPanel: (target: TileTarget, source: WidgetSource) => void;
}) {
  const { layout } = api;
  const [cols, setCols] = useState<number>(GRID_COLS);
  const [slotOpen, setSlotOpen] = useState<string | null>(null);
  const gridRef = useRef<HTMLDivElement>(null);

  // JS owns the column count: it has to, to emit coordinates.
  useEffect(() => {
    const el = gridRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver((entries) => {
      const width = entries[0]?.contentRect.width ?? el.clientWidth;
      setCols(width <= NARROW_MAX ? GRID_COLS / 2 : GRID_COLS);
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const pos = useMemo(
    () =>
      cols === GRID_COLS
        ? new Map<string, Rect>(layout.widgets.map((w) => [w.id, rectOf(w)]))
        : reflowToColumns(layout, GRID_COLS / 2),
    [layout, cols],
  );

  const showSlots = edit && !searching && cols === GRID_COLS;
  const slots = useMemo(() => (showSlots ? emptySlots(layout) : []), [layout, showSlots]);
  // A slot's id is its position, so an open popover survives unrelated edits.
  const slotKey = (r: Rect) => `${r.x},${r.y},${r.cols},${r.rows}`;
  useEffect(() => {
    if (slotOpen && !slots.some((r) => slotKey(r) === slotOpen)) setSlotOpen(null);
  }, [slots, slotOpen]);

  let used = 0;
  for (const r of pos.values()) used = Math.max(used, r.y + r.rows);
  // The add tile sits on a whole cell.
  const usedCells = Math.ceil(used / CELL) * CELL;

  const seen = new Set<string>();
  const showAdd = edit && !searching;

  return (
    <div
      className={
        "hw-grid" +
        (edit ? " is-edit" : "") +
        (searching ? " is-searching" : "") +
        (cols === GRID_COLS / 2 ? " is-narrow" : "")
      }
      style={{ "--hw-cols": cols } as CSSProperties}
      ref={gridRef}
    >
      {layout.widgets.map((w) => {
        // Keyed siblings: skipping the others keeps the search box mounted.
        if (searching && w.source !== "search") return null;
        let anchorId: string | undefined;
        if (ANCHOR_SOURCES.has(w.source) && !seen.has(w.source)) anchorId = `home-sec-${w.source}`;
        seen.add(w.source);
        const rect = pos.get(w.id);
        const style: CSSProperties | undefined = searching || !rect ? undefined : placement(rect);
        return (
          <WidgetFrame
            key={w.id}
            widget={w}
            edit={edit}
            anchorId={anchorId}
            style={style}
            api={api}
            cols={rect?.cols ?? dimsOf(w).cols}
            full={(rect?.cols ?? dimsOf(w).cols) >= cols}
            onRemove={() => api.remove(w.id)}
            onRequestPanel={onRequestPanel}
          />
        );
      })}
      {slots.map((r) => {
        const key = slotKey(r);
        const open = slotOpen === key;
        return (
          <div key={`slot${key}`} className={"hw-slot-wrap" + (open ? " is-open" : "")} style={placement(r)}>
            <button
              type="button"
              className="hw-slot"
              aria-haspopup="dialog"
              aria-expanded={open}
              onClick={() => setSlotOpen(open ? null : key)}
            >
              + Choose what goes here
            </button>
            {open ? (
              <ChangeTile
                api={api}
                target={{ kind: "fill", rect: r }}
                title="empty slot"
                onClose={() => setSlotOpen(null)}
                onPick={(source) => {
                  setSlotOpen(null);
                  onRequestPanel({ kind: "fill", rect: r }, source);
                }}
              />
            ) : null}
          </div>
        );
      })}
      {showAdd ? (
        <button
          type="button"
          className="hw-add-tile"
          style={placement({ x: 0, y: usedCells, cols: CELL, rows: CELL })}
          onClick={onAdd}
        >
          <span className="hw-add-plus" aria-hidden="true">+</span>
          Add widget
        </button>
      ) : null}
    </div>
  );
}
