// The 4-column widget grid. Edit mode adds native HTML5 drag-and-drop reorder,
// Alt+Arrow keyboard reorder, and the trailing "+ Add widget" tile.
import { useLayoutEffect, useRef, useState, type DragEvent, type KeyboardEvent } from "react";
import { WidgetFrame } from "./Widget";
import type { HomeLayoutApi } from "./useHomeLayout";

/** First widget of each source carries the tour anchor the strips used to. */
const ANCHOR_SOURCES = new Set(["search", "apps", "playground", "sessions"]);

/** Index a widget moves to for an Alt+Arrow press, or null for other keys. */
export function keyboardTarget(key: string, index: number, count: number): number | null {
  const delta = key === "ArrowLeft" || key === "ArrowUp" ? -1 : key === "ArrowRight" || key === "ArrowDown" ? 1 : 0;
  if (!delta) return null;
  const to = index + delta;
  return to < 0 || to >= count ? null : to;
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
  const [dragId, setDragId] = useState<string | null>(null);
  const [overId, setOverId] = useState<string | null>(null);
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

  const seen = new Set<string>();
  const indexOf = (id: string | null) => layout.widgets.findIndex((w) => w.id === id);

  return (
    <div className={"hw-grid" + (edit ? " is-edit" : "") + (searching ? " is-searching" : "")} ref={gridRef}>
      {layout.widgets.map((w, i) => {
        // Keyed siblings: skipping the others keeps the search box mounted.
        if (searching && w.source !== "search") return null;
        let anchorId: string | undefined;
        if (ANCHOR_SOURCES.has(w.source) && !seen.has(w.source)) anchorId = `home-sec-${w.source}`;
        seen.add(w.source);
        return (
          <WidgetFrame
            key={w.id}
            widget={w}
            edit={edit}
            anchorId={anchorId}
            dragging={dragId === w.id}
            dropTarget={overId === w.id && dragId !== w.id}
            onResize={(s) => api.resize(w.id, s)}
            onReformat={(f) => api.reformat(w.id, f)}
            onRemove={() => api.remove(w.id)}
            onDragStart={(e: DragEvent) => {
              e.dataTransfer.effectAllowed = "move";
              e.dataTransfer.setData("text/plain", w.id);
              setDragId(w.id);
            }}
            onDragOver={(e: DragEvent) => {
              if (!dragId) return;
              e.preventDefault();
              e.dataTransfer.dropEffect = "move";
              if (overId !== w.id) setOverId(w.id);
            }}
            onDrop={(e: DragEvent) => {
              e.preventDefault();
              const from = indexOf(dragId);
              if (from >= 0) api.move(from, i);
              setDragId(null);
              setOverId(null);
            }}
            onDragEnd={() => {
              setDragId(null);
              setOverId(null);
            }}
            onKeyDown={(e: KeyboardEvent) => {
              if (!e.altKey || e.target !== e.currentTarget) return;
              const to = keyboardTarget(e.key, i, layout.widgets.length);
              if (to === null) return;
              e.preventDefault();
              refocus.current = w.id;
              api.move(i, to);
            }}
          />
        );
      })}
      {edit ? (
        <button type="button" className="hw-add-tile" onClick={onAdd}>
          <span className="hw-add-plus" aria-hidden="true">+</span>
          Add widget
        </button>
      ) : null}
    </div>
  );
}
