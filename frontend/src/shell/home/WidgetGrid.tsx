// The widget grid. Every widget owns explicit half-cell units (x, y) on an 8-unit grid;
// edit mode adds a cell canvas, pointer-event drag with a snapped ghost,
// Alt+Arrow moves, and the trailing "+ Add widget" tile. Under NARROW_MAX the
// grid shows a 4-unit reflow of the same coordinates (derived, never stored)
// and moving is disabled.
import {
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent,
  type PointerEvent,
} from "react";
import { WidgetFrame } from "./Widget";
import type { HomeLayoutApi } from "./useHomeLayout";
import {
  CELL,
  GRID_COLS,
  canPlace,
  dims,
  emptyRows,
  moveByArrow,
  reflowToColumns,
  rectOf,
  type Rect,
  type Widget,
} from "./layout";

/** First widget of each source carries the tour anchor the strips used to. */
const ANCHOR_SOURCES = new Set(["search", "apps", "playground", "sessions"]);

/** At or below this grid width the layout reflows to 2 columns. */
const NARROW_MAX = 640;

/** Pointer travel before a press becomes a drag. */
const DRAG_SLOP = 4;

interface DragState {
  id: string;
  /** Which cell of the widget was grabbed. */
  grab: { dx: number; dy: number };
  start: { x: number; y: number };
  pointerId: number;
  el: HTMLElement;
  started: boolean;
}

interface Target {
  x: number;
  y: number;
  valid: boolean;
}

function placement(r: Rect): CSSProperties {
  return { gridColumn: `${r.x + 1} / span ${r.cols}`, gridRow: `${r.y + 1} / span ${r.rows}` };
}

export function WidgetGrid({
  api,
  edit,
  searching = false,
  onAdd,
}: {
  api: HomeLayoutApi;
  edit: boolean;
  /** A search query is live: only the search widget renders, full width. */
  searching?: boolean;
  onAdd: () => void;
}) {
  const { layout } = api;
  const [cols, setCols] = useState<number>(GRID_COLS);
  const [dragId, setDragId] = useState<string | null>(null);
  const [target, setTarget] = useState<Target | null>(null);
  const drag = useRef<DragState | null>(null);
  const targetRef = useRef<Target | null>(null);
  const gridRef = useRef<HTMLDivElement>(null);
  // After a keyboard move React re-inserts the node and the browser drops its
  // focus; put it back on the same widget.
  const refocus = useRef<string | null>(null);
  useLayoutEffect(() => {
    if (!refocus.current) return;
    const el = gridRef.current?.querySelector<HTMLElement>(`[data-wid="${refocus.current}"]`);
    refocus.current = null;
    el?.focus();
  });

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

  const canDrag = edit && !searching && cols === GRID_COLS;

  // A release anywhere clears an armed press, so a pointerup outside the grid
  // before DRAG_SLOP cannot leave a stuck drag behind.
  const endDragRef = useRef<() => void>(() => {});
  const onWindowUp = useRef(() => endDragRef.current()).current; // stable, so it can be removed
  const endDrag = () => {
    const d = drag.current;
    if (d) d.el.style.transform = "";
    drag.current = null;
    targetRef.current = null;
    window.removeEventListener("pointerup", onWindowUp);
    setDragId(null);
    setTarget(null);
  };

  endDragRef.current = endDrag;

  // Escape cancels a drag in progress. Capture phase on document: Home's own
  // Escape handler (leave edit mode) is a bubble listener on document, so
  // stopping propagation here keeps it from also firing during a drag.
  useEffect(() => {
    if (!dragId) return;
    const key = (e: globalThis.KeyboardEvent) => {
      if (e.key !== "Escape") return;
      e.preventDefault();
      e.stopPropagation();
      endDrag();
    };
    document.addEventListener("keydown", key, true);
    return () => document.removeEventListener("keydown", key, true);
  }, [dragId]);

  // Leaving edit mode (or entering search / narrow) mid-drag drops it.
  useEffect(() => {
    if (!canDrag && drag.current) endDrag();
  }, [canDrag]);

  const widgetOf = (id: string): Widget | undefined => layout.widgets.find((w) => w.id === id);

  const onWidgetPointerDown = (w: Widget, e: PointerEvent<HTMLElement>) => {
    if (!canDrag || e.button !== 0) return;
    if ((e.target as Element).closest('button, a, input, textarea, [role="dialog"]')) return;
    const el = e.currentTarget;
    const rect = el.getBoundingClientRect();
    const d = dims(w.size);
    const grab = {
      dx: Math.min(d.cols - 1, Math.max(0, Math.floor((e.clientX - rect.left) / (rect.width / d.cols)))),
      dy: Math.min(d.rows - 1, Math.max(0, Math.floor((e.clientY - rect.top) / (rect.height / d.rows)))),
    };
    drag.current = { id: w.id, grab, start: { x: e.clientX, y: e.clientY }, pointerId: e.pointerId, el, started: false };
    window.addEventListener("pointerup", onWindowUp);
  };

  const onPointerMove = (e: PointerEvent<HTMLDivElement>) => {
    const d = drag.current;
    if (!d) return;
    if (e.buttons === 0) {
      endDrag();
      return;
    }
    if (!d.started) {
      if (Math.hypot(e.clientX - d.start.x, e.clientY - d.start.y) < DRAG_SLOP) return;
      d.started = true;
      try {
        d.el.setPointerCapture(d.pointerId);
      } catch {
        // Capture is best-effort; moves still reach the grid while over it.
      }
      setDragId(d.id);
    }
    d.el.style.transform = `translate(${e.clientX - d.start.x}px, ${e.clientY - d.start.y}px)`;
    const w = widgetOf(d.id);
    if (!w) return;
    const cell = document.elementFromPoint(e.clientX, e.clientY)?.closest<HTMLElement>("[data-x]");
    let next: Target | null = null;
    if (cell) {
      // Nearest half cell: the pointer's offset inside the hovered whole cell rounds to 0, 1 or 2 units.
      const cr = cell.getBoundingClientRect();
      const hx = Math.min(CELL, Math.max(0, Math.round((e.clientX - cr.left) / (cr.width / CELL))));
      const hy = Math.min(CELL, Math.max(0, Math.round((e.clientY - cr.top) / (cr.height / CELL))));
      const x = Number(cell.dataset.x) + hx - d.grab.dx;
      const y = Number(cell.dataset.y) + hy - d.grab.dy;
      const { cols: c, rows: r } = dims(w.size);
      next = { x, y, valid: canPlace(layout.widgets, { x, y, cols: c, rows: r }, d.id) };
    }
    const prev = targetRef.current;
    if (prev?.x !== next?.x || prev?.y !== next?.y || prev?.valid !== next?.valid) {
      targetRef.current = next;
      setTarget(next);
    }
  };

  const onPointerUp = () => {
    const d = drag.current;
    if (!d) return;
    const t = targetRef.current;
    if (d.started && t?.valid) api.place(d.id, t.x, t.y);
    endDrag();
  };

  const dragged = dragId ? widgetOf(dragId) : undefined;
  const draggedDims = dragged ? dims(dragged.size) : null;

  let used = 0;
  for (const r of pos.values()) used = Math.max(used, r.y + r.rows);
  // Canvas and add tile sit on whole cells.
  const usedCells = Math.ceil(used / CELL) * CELL;
  const canvasRows = Math.ceil((used + (draggedDims ? draggedDims.rows : CELL)) / CELL) * CELL;

  const seen = new Set<string>();
  const cells: JSX.Element[] = [];
  if (edit && !searching) {
    for (let y = 0; y < canvasRows; y += CELL) {
      for (let x = 0; x < cols; x += CELL) {
        cells.push(
          <div
            key={`c${x},${y}`}
            className="hw-cell"
            data-x={x}
            data-y={y}
            style={placement({ x, y, cols: CELL, rows: CELL })}
            aria-hidden="true"
          />,
        );
      }
    }
  }

  const gaps: JSX.Element[] = [];
  if (!edit && !searching) {
    for (const y of emptyRows([...pos.values()])) {
      gaps.push(<div key={`g${y}`} className="hw-row-gap" aria-hidden="true" style={placement({ x: 0, y, cols, rows: 1 })} />);
    }
  }

  const showAdd = edit && !searching;

  return (
    <div
      className={
        "hw-grid" +
        (edit ? " is-edit" : "") +
        (searching ? " is-searching" : "") +
        (dragId ? " is-dragging" : "") +
        (canDrag ? " can-drag" : "") +
        (cols === GRID_COLS / 2 ? " is-narrow" : "")
      }
      style={{ "--hw-cols": cols } as CSSProperties}
      ref={gridRef}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerCancel={endDrag}
    >
      {cells}
      {gaps}
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
            dragging={dragId === w.id}
            style={style}
            layout={layout}
            onResize={(s) => api.resize(w.id, s)}
            onReformat={(f) => api.reformat(w.id, f)}
            onRemove={() => api.remove(w.id)}
            onPointerDown={(e) => onWidgetPointerDown(w, e)}
            onKeyDown={(e: KeyboardEvent) => {
              if (!e.altKey || e.target !== e.currentTarget || cols !== GRID_COLS) return;
              const next = moveByArrow(layout, w.id, e.key);
              if (next === layout) return;
              e.preventDefault();
              refocus.current = w.id;
              api.arrow(w.id, e.key);
            }}
          />
        );
      })}
      {target && draggedDims ? (
        <div
          className={"hw-ghost " + (target.valid ? "is-valid" : "is-invalid")}
          style={placement({ x: target.x, y: target.y, cols: draggedDims.cols, rows: draggedDims.rows })}
          aria-hidden="true"
        />
      ) : null}
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
