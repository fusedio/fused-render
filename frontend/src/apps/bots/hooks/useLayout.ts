// Mounts the layout plumbing once <main> exists, and hands out the gutter handlers.
import { useLayoutEffect } from "react";
import type { PointerEvent as ReactPointerEvent } from "react";
import { initLayout, resetSide, startGutterDrag, type Side } from "../lib/layout";

export interface GutterProps {
  onPointerDown: (e: ReactPointerEvent<HTMLDivElement>) => void;
  onDoubleClick: () => void;
}

export const gutterProps = (side: Side): GutterProps => ({
  onPointerDown: (e) => startGutterDrag(side, e.currentTarget, e.nativeEvent),
  onDoubleClick: () => resetSide(side),
});

/** Call once in the component that renders <main> (App). */
export function useLayout(): { gutter: (side: Side) => GutterProps } {
  useLayoutEffect(() => initLayout(), []);
  return { gutter: gutterProps };
}
