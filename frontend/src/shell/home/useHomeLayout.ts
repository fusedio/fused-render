// Home layout state: loads the saved layout (default when none), exposes the
// pure mutators from layout.ts, and PUTs the whole document after each change.
// Optimistic — a failed PUT keeps the local state and raises a notification.
import { useCallback, useEffect, useRef, useState } from "react";
import { getHomeLayout, putHomeLayout } from "@platform/lib/api";
import { notify } from "@platform/lib/notifications";
import {
  addWidget,
  fillSlot,
  normalizeLayout,
  presetLayout,
  removeWidget,
  setFormat,
  setSize,
  setSort,
  setTasksShow,
  swapSource,
  defaultLayout,
  type AppsSort,
  type TasksShow,
  type HomeLayout,
  type PresetId,
  type Rect,
  type TileOpts,
  type WidgetFormat,
  type WidgetSize,
  type WidgetSource,
} from "./layout";

export interface HomeLayoutApi {
  layout: HomeLayout;
  /** False until the first GET settles — the grid waits so a saved layout never
      flashes the default one. */
  loaded: boolean;
  /** Replace the whole layout with a preset (fresh ids). */
  applyPreset: (id: PresetId, opts?: { folderId?: string }) => void;
  /** Put the given layout back (undo of a preset). */
  restore: (layout: HomeLayout) => void;
  /** Show another source in a tile, keeping its rectangle. */
  swap: (id: string, source: WidgetSource, opts?: TileOpts) => void;
  /** Put a new tile of `source` on exactly `rect`, an empty slot. */
  fill: (rect: Rect, source: WidgetSource, opts?: TileOpts) => void;
  add: (source: WidgetSource, opts?: { folderId?: string; appPath?: string; format?: WidgetFormat; size?: WidgetSize; sort?: AppsSort; show?: TasksShow }) => void;
  remove: (id: string) => void;
  reformat: (id: string, format: WidgetFormat) => void;
  /** Resize a tile to a preset; a no-op when it does not fit where the tile sits. */
  resize: (id: string, size: WidgetSize) => void;
  resort: (id: string, sort: AppsSort) => void;
  reshow: (id: string, show: TasksShow) => void;
}

export function useHomeLayout(): HomeLayoutApi {
  const [layout, setLayout] = useState<HomeLayout>(defaultLayout);
  const [loaded, setLoaded] = useState(false);
  const ref = useRef(layout);
  // PUTs run strictly in order so the last edit is the last write.
  const tail = useRef<Promise<unknown>>(Promise.resolve());

  useEffect(() => {
    let alive = true;
    getHomeLayout().then(
      (r) => {
        if (!alive) return;
        const next = r.exists ? normalizeLayout(r.layout) : defaultLayout();
        ref.current = next;
        setLayout(next);
        setLoaded(true);
      },
      () => {
        // Unreadable store: show the default; the first edit writes it anew.
        if (alive) setLoaded(true);
      },
    );
    return () => {
      alive = false;
    };
  }, []);

  const commit = useCallback((next: HomeLayout) => {
    if (next === ref.current) return;
    ref.current = next;
    setLayout(next);
    tail.current = tail.current.then(() =>
      putHomeLayout(next).catch((e: Error) => {
        notify({
          title: "Couldn't save your Home layout",
          detail: e?.message || "The change is kept for now but won't survive a restart.",
          tone: "error",
        });
      }),
    );
  }, []);

  return {
    layout,
    loaded,
    applyPreset: useCallback((id, opts) => commit(presetLayout(id, opts)), [commit]),
    restore: useCallback((l) => commit(normalizeLayout(l)), [commit]),
    swap: useCallback((id, source, opts) => commit(swapSource(ref.current, id, source, opts)), [commit]),
    fill: useCallback((rect, source, opts) => commit(fillSlot(ref.current, rect, source, opts)), [commit]),
    add: useCallback((source, opts) => commit(addWidget(ref.current, source, opts)), [commit]),
    remove: useCallback((id) => commit(removeWidget(ref.current, id)), [commit]),
    reformat: useCallback((id, format) => commit(setFormat(ref.current, id, format)), [commit]),
    resize: useCallback((id, size) => commit(setSize(ref.current, id, size)), [commit]),
    resort: useCallback((id, sort) => commit(setSort(ref.current, id, sort)), [commit]),
    reshow: useCallback((id, show) => commit(setTasksShow(ref.current, id, show)), [commit]),
  };
}
