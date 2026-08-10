// The pointer half of rubber-band selection: where a press is allowed to start
// one, the drag itself (with the listing auto-scrolling at its edges), and the
// rectangle the caller paints. Every DECISION is marquee.ts, which is pure and
// tested — a headless test cannot see a drag, so nothing that only the browser
// can see is allowed to decide anything.
//
// GESTURE SPLIT. Which of the two press-and-move gestures a press begins is
// drag-drop's `pressGesture` — the same rule useRowDrag wires `draggable` from,
// read here so the hit test and the drag source cannot drift apart. In short: a
// row's NAME/ICON drags, a SELECTED row drags anywhere, and the rest of a row —
// its gutter, the space after the name, the size and modified columns — is
// marquee surface, along with the background below the rows.
//
// The first version of this split marquees only on the background, which sounds
// right and is not: rows span the full width, so the only marquee surface was
// the empty space below the last row and there was no way to rubber-band a
// group in the MIDDLE of a populated list.
//
// A press that never travels MARQUEE_DRAG_SLOP is neither gesture — it is the
// click the listing already had. There is no arbitration afterwards and in
// particular no timer, so single-click-select and double-click-open are
// untouched.
//
// COORDINATES are the scroller's CONTENT space (viewport offset + scrollTop),
// not the viewport's. That is what lets the listing scroll under a live drag
// without the box or the row bands going stale, and it is also exactly the
// space an absolutely-positioned child of the scroller is laid out in, so the
// painted rectangle needs no conversion at all.
import { useEffect, useRef, useState } from "react";
import { pressGesture } from "@apps/explorer/listing/drag-drop";
import { ROW_HANDLE_CLASS } from "@apps/explorer/listing/useRowDrag";
import {
  autoScrollStep,
  marqueeBox,
  marqueeHits,
  passedDragSlop,
  rowBands,
  type Box,
  type Point,
  type RowBand,
} from "@apps/explorer/listing/marquee";

// Row geometry for the whole listing, measured ONCE per drag from one rendered
// row (see rowBands on why the model beats a per-row DOM read). Returns [] when
// there is nothing rendered to measure, which makes the marquee a no-op rather
// than a selection of everything.
function measureBands(scroller: HTMLElement, paths: string[]): RowBand[] {
  const row = scroller.querySelector<HTMLElement>("table.listing-table tr.row");
  if (!row) return [];
  const sRect = scroller.getBoundingClientRect();
  const rRect = row.getBoundingClientRect();
  const left = rRect.left - sRect.left + scroller.scrollLeft;
  return rowBands(paths, {
    firstTop: rRect.top - sRect.top + scroller.scrollTop,
    height: rRect.height,
    left,
    right: left + rRect.width,
  });
}

// Does this press start a marquee? The row-vs-background half is pressGesture's
// (shared with the drag); everything a CONTROL owns is excluded first, because
// a column header sorts and a Load-more button clicks — neither is a surface
// you can sweep from.
function startsMarquee(target: EventTarget | null): boolean {
  const el = target as HTMLElement | null;
  if (!el || typeof el.closest !== "function") return false;
  if (el.closest("thead, button, input, a")) return false;
  const row = el.closest("tr.row");
  return (
    pressGesture({
      onRow: !!row,
      onName: !!el.closest("." + ROW_HANDLE_CLASS),
      rowSelected: !!row?.classList.contains("selected"),
    }) === "marquee"
  );
}

export function useMarquee({
  scrollRef,
  navRows,
  selectedPaths,
  selectPaths,
  enabled = true,
}: {
  scrollRef: React.RefObject<HTMLDivElement>;
  // The rendered row order — the same list the arrow keys walk, so a marquee
  // and a Shift+arrow range can never disagree about what is where.
  navRows: string[];
  // The selection as it stands, which an additive (Shift / Cmd) sweep unions
  // with. Read through a ref so the once-registered pointer handlers see the
  // value at DRAG START rather than the one they closed over.
  selectedPaths: string[];
  selectPaths: (paths: string[]) => void;
  enabled?: boolean;
}) {
  // The rectangle being dragged right now, in content coordinates, or null when
  // no marquee is in flight. This is the ONLY marquee state that re-renders —
  // the drag's own bookkeeping lives in refs so a pointermove costs one paint,
  // not a cascade.
  const [box, setBox] = useState<Box | null>(null);

  const rowsRef = useRef<string[]>([]);
  rowsRef.current = navRows;
  const selRef = useRef<string[]>([]);
  selRef.current = selectedPaths;
  const selectRef = useRef(selectPaths);
  selectRef.current = selectPaths;

  // Everything about the drag in flight. `active` is false until the press has
  // travelled far enough to be a drag at all (before that it is still a click).
  const drag = useRef<{
    pointerId: number;
    origin: Point; // content coords
    // The pointer's last VIEWPORT position: the edge test needs it, and so does
    // the auto-scroll frame, which re-sweeps from a pointer that hasn't moved.
    clientX: number;
    clientY: number;
    additive: boolean;
    base: string[];
    bands: RowBand[];
    active: boolean;
  } | null>(null);
  // The auto-scroll frame loop's handle, so it can be cancelled from anywhere.
  const rafRef = useRef(0);

  // Content-space point for a pointer event.
  const contentPoint = (scroller: HTMLElement, clientX: number, clientY: number): Point => {
    const r = scroller.getBoundingClientRect();
    return {
      x: clientX - r.left + scroller.scrollLeft,
      y: clientY - r.top + scroller.scrollTop,
    };
  };

  // One update of box + selection from the pointer's current position. Called
  // from pointermove AND from the auto-scroll frame — scrolling moves the rows
  // under a stationary pointer, which is a change to the swept set even though
  // the pointer never moved.
  const sweepTo = (scroller: HTMLElement, clientX: number, clientY: number) => {
    const d = drag.current;
    if (!d) return;
    const at = contentPoint(scroller, clientX, clientY);
    if (!d.active) {
      if (!passedDragSlop(d.origin, at)) return;
      d.active = true;
      // Measured now rather than on pointerdown: a press that turns out to be
      // a click should cost nothing at all.
      d.bands = measureBands(scroller, rowsRef.current);
    }
    const next = marqueeBox(d.origin, at);
    setBox(next);
    selectRef.current(marqueeHits(next, d.bands, { additive: d.additive, base: d.base }));
  };

  const onPointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!enabled) return;
    // Left button only: the right button opens the background context menu, and
    // the middle one is the browser's.
    if (e.button !== 0) return;
    if (!startsMarquee(e.target)) return;
    const scroller = scrollRef.current;
    if (!scroller) return;
    // Stops the browser painting a text selection across the listing as the
    // pointer sweeps (the rows' own user-select:none is not enough — a drag
    // that STARTS outside a row still sets an endpoint).
    e.preventDefault();
    // Capture keeps the sweep alive when the pointer leaves the scroller (over
    // the preview pane, off the window edge). It throws for a pointer id the
    // browser has no active pointer for — which a synthetic event is — and the
    // drag works without it, so a failure here must not take the gesture down
    // with it.
    try {
      scroller.setPointerCapture(e.pointerId);
    } catch {
      /* no capture; the listeners below are on the scroller either way */
    }
    drag.current = {
      pointerId: e.pointerId,
      origin: contentPoint(scroller, e.clientX, e.clientY),
      clientX: e.clientX,
      clientY: e.clientY,
      // Shift or Cmd/Ctrl UNIONS with what is already selected; a bare sweep
      // replaces it. Read once, at the press: releasing the modifier mid-drag
      // must not silently change what the gesture meant when it started.
      additive: e.shiftKey || e.metaKey || e.ctrlKey,
      base: [...selRef.current],
      bands: [],
      active: false,
    };
  };

  // pointermove / pointerup / auto-scroll, registered on the SCROLLER for the
  // life of the component rather than per drag: pointer capture already routes
  // every move here, and a listener that exists only while a drag is in flight
  // is a listener that can be orphaned by an unmount mid-gesture.
  useEffect(() => {
    const scroller = scrollRef.current;
    if (!scroller || !enabled) return;

    const stopScrollLoop = () => {
      if (rafRef.current) cancelAnimationFrame(rafRef.current);
      rafRef.current = 0;
    };

    // Edge auto-scroll: while the pointer sits in (or past) the top/bottom
    // band, scroll and re-sweep every frame. It runs only while a drag is
    // ACTIVE, and stops itself the moment the step is zero, so a marquee in the
    // middle of the listing costs no frames at all.
    const scrollLoop = () => {
      const d = drag.current;
      if (!d || !d.active) return stopScrollLoop();
      const r = scroller.getBoundingClientRect();
      const step = autoScrollStep(d.clientY, { top: r.top, bottom: r.bottom });
      if (step === 0) return stopScrollLoop();
      const before = scroller.scrollTop;
      scroller.scrollTop += step;
      // At either end of the listing the scroll cannot move; re-sweeping would
      // be identical work forever, so let the loop idle until the pointer does
      // something.
      if (scroller.scrollTop !== before) sweepTo(scroller, d.clientX, d.clientY);
      rafRef.current = requestAnimationFrame(scrollLoop);
    };

    const onMove = (ev: PointerEvent) => {
      const d = drag.current;
      if (!d || ev.pointerId !== d.pointerId) return;
      d.clientY = ev.clientY;
      d.clientX = ev.clientX;
      sweepTo(scroller, ev.clientX, ev.clientY);
      if (d.active && !rafRef.current) rafRef.current = requestAnimationFrame(scrollLoop);
    };

    const onUp = (ev: PointerEvent) => {
      const d = drag.current;
      if (!d || ev.pointerId !== d.pointerId) return;
      stopScrollLoop();
      drag.current = null;
      setBox(null);
      if (scroller.hasPointerCapture(ev.pointerId)) scroller.releasePointerCapture(ev.pointerId);
      // Nothing to commit on release: the selection has been live the whole
      // drag, and the `?sel=` write is already debounced (useListingSelection),
      // so a sweep spends one history.replaceState when it settles rather than
      // one per frame.
    };

    scroller.addEventListener("pointermove", onMove);
    scroller.addEventListener("pointerup", onUp);
    scroller.addEventListener("pointercancel", onUp);
    return () => {
      stopScrollLoop();
      scroller.removeEventListener("pointermove", onMove);
      scroller.removeEventListener("pointerup", onUp);
      scroller.removeEventListener("pointercancel", onUp);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled]);

  return { onPointerDown, box };
}
