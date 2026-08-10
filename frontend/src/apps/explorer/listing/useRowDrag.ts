// The DOM half of drag-to-move: the handlers a listing row, the listing
// background and the sidebar hang off, plus the drag image and the hovered-
// target highlight. Every DECISION it makes belongs to listing/drag-drop.ts
// (which is pure and tested); what is here is wiring, kept thin on purpose —
// a headless test can't see a drag, so anything it can't see should not be
// deciding anything.
//
// Two things are worth knowing before reading the handlers:
//
//   • dragover must call preventDefault() to ALLOW a drop, so "don't allow it"
//     is simply not calling it. That is also what paints the refused cursor, so
//     an invalid target gets the right pointer for free — the class this hook
//     adds is the visible half of the same verdict, not a second decision.
//   • WHERE a drag may start is `draggable`, set in ONE place from drag-drop's
//     pressStartsDrag: a row is a drag source exactly while it is SELECTED,
//     and nowhere else. A press on any part of an unselected row selects it,
//     and sweeps if it travels (useMarquee reads the same rule inverted). The
//     name cell used to be a permanent handle as well; it was taking the
//     pixels drag-to-select needs, and it is gone.
//   • dragover fires continuously while the pointer moves, so the verdict has
//     to be cheap and synchronous. It is: the dragged entries live in the
//     module-level in-flight store (drag-drop.ts), not in the dataTransfer the
//     browser hides from us mid-drag.
import { useRef, useState } from "react";
import { dirname, normDir } from "@apps/explorer/lib/fs-actions";
import { basename } from "@platform/lib/format";
import {
  FS_DRAG_MIME,
  pressStartsDrag,
  carriesFsDrag,
  clearFsDrag,
  dragPathsFor,
  dropIsValid,
  encodeDragPaths,
  fsDragInFlight,
  startFsDrag,
  type DropTarget,
} from "@apps/explorer/listing/drag-drop";
import type { RowCtx } from "@apps/explorer/listing/types";

// The ghost that follows the cursor. The browser's default is a snapshot of the
// dragged element — one <tr> of a table, which for a multi-row drag would show
// exactly one of the things being moved and give no hint that the other four
// are coming. This is a name (or a count) with a folder-ish tint, built off
// screen and thrown away once the browser has taken its snapshot.
function makeDragImage(label: string, count: number): HTMLElement {
  const el = document.createElement("div");
  el.className = "fs-drag-image";
  el.textContent = label;
  if (count > 1) {
    const badge = document.createElement("span");
    badge.className = "fs-drag-count";
    badge.textContent = String(count);
    el.appendChild(badge);
  }
  document.body.appendChild(el);
  return el;
}

export interface RowDragHandlers {
  draggable: boolean;
  onDragStart: (e: React.DragEvent) => void;
  onDragOver: (e: React.DragEvent) => void;
  onDragLeave: (e: React.DragEvent) => void;
  onDrop: (e: React.DragEvent) => void;
  onDragEnd: () => void;
}

export function useRowDrag({
  base,
  selectedPaths,
  rowCtxByPath,
  onMove,
}: {
  // The listed folder, which is also the background target ("move these here").
  base: string;
  // The current selection in RENDERED order — a drag that starts inside it
  // carries all of it (dragPathsFor).
  selectedPaths: string[];
  rowCtxByPath: ReadonlyMap<string, RowCtx>;
  onMove: (paths: string[], targetDir: string) => void;
}) {
  // The target under the cursor right now, and whether it would accept the
  // drop. One slot: there is one pointer, so at most one target is hovered.
  const [drop, setDrop] = useState<{ path: string; ok: boolean } | null>(null);
  // The off-screen drag image, removed on the first frame after dragstart (the
  // browser has snapshotted it by then) and on dragend as a backstop.
  const ghostRef = useRef<HTMLElement | null>(null);

  const dropGhost = () => {
    ghostRef.current?.remove();
    ghostRef.current = null;
  };

  const endDrag = () => {
    clearFsDrag();
    dropGhost();
    setDrop(null);
  };

  const onDragStart = (e: React.DragEvent, row: RowCtx) => {
    // The pressed row is ALREADY selected by the time a drag can start: the
    // selection is decided on pointerdown now (Listing's onRowPointerDown), and
    // the press that begins a drag is the same press. This used to call
    // selectOnly here for a row outside the selection; that is the pointerdown
    // handler's job and doing it twice would be two places deciding what a
    // press selects.
    //
    // dragPathsFor still decides WHAT TRAVELS, which is the separate question:
    // a row inside the selection carries all of it, a row outside carries only
    // itself. Both readings of `selectedPaths` agree here — whether or not
    // React has re-rendered between the press and the drag, an unselected row
    // yields just itself and a selected one yields the selection.
    const paths = dragPathsFor(row.path, selectedPaths);
    startFsDrag(
      paths.map((p) => {
        const ctx = rowCtxByPath.get(p);
        return { path: p, parentDir: ctx ? ctx.parentDir : normDir(dirname(p)) };
      }),
    );
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData(FS_DRAG_MIME, encodeDragPaths(paths));
    // Firefox refuses to start a drag with no text/plain, and the paths are
    // also the sensible thing to hand a terminal or an editor.
    e.dataTransfer.setData("text/plain", paths.join("\n"));
    const ghost = makeDragImage(paths.length === 1 ? basename(paths[0]) : `${paths.length} items`, paths.length);
    ghostRef.current = ghost;
    e.dataTransfer.setDragImage(ghost, 12, 12);
    requestAnimationFrame(dropGhost);
  };

  // Shared by every drop target — a row, the background, a sidebar entry. The
  // caller supplies WHAT the target is; the verdict and the highlight are the
  // same everywhere.
  const overTarget = (e: React.DragEvent, target: DropTarget) => {
    if (!carriesFsDrag(e.dataTransfer.types)) return;
    const verdict = dropIsValid(fsDragInFlight(), target);
    // Stop here on a row so the listing background (which is also a target)
    // doesn't light up behind the row the cursor is actually over.
    e.stopPropagation();
    if (verdict.ok) {
      e.preventDefault(); // the whole of "this drop is allowed"
      e.dataTransfer.dropEffect = "move";
    }
    setDrop((prev) =>
      prev && prev.path === target.path && prev.ok === verdict.ok
        ? prev
        : { path: target.path, ok: verdict.ok },
    );
  };

  const leaveTarget = (target: DropTarget) => {
    setDrop((prev) => (prev && prev.path === target.path ? null : prev));
  };

  const dropOn = (e: React.DragEvent, target: DropTarget) => {
    if (!carriesFsDrag(e.dataTransfer.types)) return;
    const dragged = fsDragInFlight();
    const verdict = dropIsValid(dragged, target);
    e.stopPropagation();
    if (!verdict.ok) {
      endDrag();
      return;
    }
    e.preventDefault();
    const paths = dragged.map((d) => d.path);
    endDrag();
    onMove(paths, verdict.dir);
  };

  // `selected` is the whole of whether this row is a drag source.
  const rowDrag = (row: RowCtx, selected: boolean): RowDragHandlers => ({
    draggable: pressStartsDrag({ rowSelected: selected }),
    onDragStart: (e) => onDragStart(e, row),
    onDragOver: (e) => overTarget(e, { path: row.path, isDir: row.isDir }),
    onDragLeave: () => leaveTarget({ path: row.path, isDir: row.isDir }),
    onDrop: (e) => dropOn(e, { path: row.path, isDir: row.isDir }),
    onDragEnd: endDrag,
  });

  // The listing's own background means THIS FOLDER. It is a target only when
  // that isn't a no-op — dragging rows around inside the folder they already
  // live in is the common case, and lighting the whole listing up for it would
  // promise a move that cannot happen. dropIsValid says so ("already-there"),
  // so nothing extra is needed here beyond pointing it at `base`.
  const backgroundTarget: DropTarget = { path: normDir(base), isDir: true };
  const backgroundDrag = {
    onDragOver: (e: React.DragEvent) => overTarget(e, backgroundTarget),
    onDragLeave: () => leaveTarget(backgroundTarget),
    onDrop: (e: React.DragEvent) => dropOn(e, backgroundTarget),
  };

  // The class a target wears while the cursor is over it: accepted, or refused
  // (which the cursor already says — the class is what says WHICH row refused).
  const dropClass = (path: string): string =>
    drop && drop.path === path ? (drop.ok ? " drop-into" : " drop-reject") : "";

  return {
    rowDrag,
    backgroundDrag,
    dropClass,
    backgroundActive: drop !== null && drop.path === backgroundTarget.path && drop.ok,
  };
}
