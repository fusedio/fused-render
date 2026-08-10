// The listing's half of drag-to-move: what a press picks up, and who performs
// the drop. Everything the GESTURE does — hit-testing, the ghost, the drop
// highlight, Escape, auto-scroll — is listing/row-drag.ts, which is
// module-level because a spring-loaded breadcrumb remounts this component
// mid-drag; everything it DECIDES is listing/drag-drop.ts, which is pure and
// tested.
//
// This hook is deliberately two small things:
//
//   • startMoveDrag — called by the press arbiter (useMarquee) when a press
//     lands on a row that WAS ALREADY SELECTED. It turns the pressed row into
//     the payload (dragPathsFor: a row inside the selection carries all of it)
//     and hands the gesture over.
//   • the mover registration — the drop is performed by the listing that owns
//     the TARGET, resolved from the DOM at drop time, so a drop into a folder
//     the spring-load has just opened refreshes the listing it landed in.
//
// The rows themselves have no drag handlers at all any more, and no
// `draggable`: they declare what they ACCEPT with data attributes
// (row-drag.ts's DOM protocol) and nothing else. That is what a drag source
// being a re-rendered React attribute cost us — see row-drag.ts's header.
import { useEffect, useRef } from "react";
import { dirname, normDir } from "@apps/explorer/lib/fs-actions";
import { basename } from "@platform/lib/format";
import { dragPathsFor } from "@apps/explorer/listing/drag-drop";
import {
  beginRowDrag,
  registerListingMover,
  type MoveOpts,
} from "@apps/explorer/listing/row-drag";
import type { RowCtx } from "@apps/explorer/listing/types";

// What the arbiter knows about the press it is handing over.
export interface MoveDragPress {
  path: string;
  pointerId: number;
  clientX: number;
  clientY: number;
}

export function useRowDrag({
  selectedPaths,
  rowCtxByPath,
  scrollRef,
  onMove,
}: {
  // The current selection in RENDERED order — a drag that starts inside it
  // carries all of it (dragPathsFor), top to bottom.
  selectedPaths: string[];
  rowCtxByPath: ReadonlyMap<string, RowCtx>;
  scrollRef: React.RefObject<HTMLDivElement>;
  onMove: (paths: string[], targetDir: string, opts: MoveOpts) => void;
}) {
  // Read through refs: the gesture outlives the render it started in, and the
  // registered mover is called by a module that has no idea this component
  // re-rendered.
  const selRef = useRef<string[]>([]);
  selRef.current = selectedPaths;
  const ctxRef = useRef(rowCtxByPath);
  ctxRef.current = rowCtxByPath;
  const moveRef = useRef(onMove);
  moveRef.current = onMove;

  useEffect(() => {
    const scroller = scrollRef.current;
    if (!scroller) return;
    return registerListingMover(scroller, (paths, dir, opts) =>
      moveRef.current(paths, dir, opts),
    );
  }, [scrollRef]);

  const startMoveDrag = (press: MoveDragPress) => {
    const paths = dragPathsFor(press.path, selRef.current);
    beginRowDrag({
      pointerId: press.pointerId,
      clientX: press.clientX,
      clientY: press.clientY,
      // parentDir is what makes "drop onto the folder it is already in" a no-op
      // rather than a move; every rendered row already carries it.
      items: paths.map((p) => {
        const ctx = ctxRef.current.get(p);
        return { path: p, parentDir: ctx ? ctx.parentDir : normDir(dirname(p)) };
      }),
      names: paths.map((p) => ctxRef.current.get(p)?.name ?? basename(p)),
      scroller: scrollRef.current,
    });
  };

  return { startMoveDrag };
}
