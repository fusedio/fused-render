// Home layout state: loads the saved layout (default when none), exposes the
// pure mutators from layout.ts, and PUTs the whole document after each change.
// Optimistic — a failed PUT keeps the local state and raises a notification.
import { useCallback, useEffect, useRef, useState } from "react";
import { getHomeLayout, putHomeLayout } from "@platform/lib/api";
import { notify } from "@platform/lib/notifications";
import {
  addWidget,
  defaultLayout,
  moveWidget,
  normalizeLayout,
  removeWidget,
  setFormat,
  setSize,
  type HomeLayout,
  type WidgetFormat,
  type WidgetSize,
  type WidgetSource,
} from "./layout";

export interface HomeLayoutApi {
  layout: HomeLayout;
  /** False until the first GET settles — the grid waits so a saved layout never
      flashes the default one. */
  loaded: boolean;
  move: (from: number, to: number) => void;
  add: (source: WidgetSource, opts?: { folderId?: string; appPath?: string; format?: WidgetFormat; size?: WidgetSize }) => void;
  remove: (id: string) => void;
  resize: (id: string, size: WidgetSize) => void;
  reformat: (id: string, format: WidgetFormat) => void;
  reset: () => void;
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
    move: useCallback((from, to) => commit(moveWidget(ref.current, from, to)), [commit]),
    add: useCallback((source, opts) => commit(addWidget(ref.current, source, opts)), [commit]),
    remove: useCallback((id) => commit(removeWidget(ref.current, id)), [commit]),
    resize: useCallback((id, size) => commit(setSize(ref.current, id, size)), [commit]),
    reformat: useCallback((id, format) => commit(setFormat(ref.current, id, format)), [commit]),
    reset: useCallback(() => commit(defaultLayout()), [commit]),
  };
}
