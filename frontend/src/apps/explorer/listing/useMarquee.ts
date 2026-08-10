// SWEEP TO SELECT: press on a row's dead space (or the background) and drag,
// and every row the swept region crosses becomes selected. The pointer half —
// where a sweep may start, the drag itself, the edge auto-scroll. Every
// DECISION is marquee.ts, which is pure and tested; a headless test cannot see
// a drag, so nothing only the browser can see is allowed to decide anything.
//
// NOTHING IS DRAWN. There was a rubber band once, and it was the part the user
// asked to lose: the rows highlighting as the pointer crosses them already say
// what is selected, and a rectangle over the top of them said it twice. So the
// swept region is a hit-test region only — computed, never rendered. That is
// also why this hook holds no React state at all: a sweep re-renders the table
// when the SELECTION changes and not once per pointermove.
//
// THIS HOOK IS THE GESTURE ARBITER, and there is only one. Its pointerdown is
// registered in the CAPTURE phase (Listing's onPointerDownCapture), which is
// what makes the whole thing work: it runs BEFORE the row's own pointerdown,
// so the selection it reads is the selection AS IT STOOD BEFORE THIS PRESS. It
// takes that snapshot, asks drag-drop's `pressStartsDrag` once, and the answer
// is final for the life of the gesture —
//
//   was the pressed row ALREADY selected?  → hand off a MOVE-DRAG (row-drag.ts)
//   anything else                          → SWEEP from here
//
// The snapshot is the fix, not a nicety. A press on an unselected row SELECTS
// it, so a rule that consults the LIVE selection any time after the press sees
// a selected row and calls every sweep a move-drag. That is exactly what the
// `draggable` attribute was — a flag the browser read when the movement began,
// a re-render too late — and it is why the native drag API is out of the row
// drag entirely (row-drag.ts's header).
//
// A press that never travels MARQUEE_DRAG_SLOP is neither gesture: it is the
// plain click that selects one row. ONE threshold decides all three, so no
// arbitration afterwards and no timer, and single-click-select and
// double-click-open are untouched.
//
// COORDINATES are the scroller's CONTENT space (viewport offset + scrollTop),
// not the viewport's. That is what lets the listing scroll under a live sweep
// without the region or the row bands going stale.
import { useEffect, useRef } from "react";
import { pressStartsDrag } from "@apps/explorer/listing/drag-drop";
import { DROP_PATH_ATTR } from "@apps/explorer/listing/row-drag";
import {
  autoScrollStep,
  marqueeBox,
  marqueeHits,
  passedDragSlop,
  rowBands,
  type Point,
  type RowBand,
} from "@apps/explorer/listing/marquee";

// Row geometry for the whole listing, measured ONCE per drag from one rendered
// row (see rowBands on why the model beats a per-row DOM read). Returns [] when
// there is nothing rendered to measure, which `marqueeHits` reads as "leave the
// selection alone" — a genuine no-op, rather than a selection of everything or
// (as it was) a clearing of what the user had.
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

// The row a press landed on, or null for the background — and `null` for a
// press this listing must not claim at all. Anything a CONTROL owns is excluded
// first: a column header sorts, a Load-more button clicks, neither is a surface
// you can sweep or drag from.
//
// The row's path comes off the data attribute it already carries as a drop
// target (row-drag.ts's DOM protocol), so there is one attribute and not two.
function pressedRow(target: EventTarget | null): { row: HTMLElement | null } | null {
  const el = target as HTMLElement | null;
  if (!el || typeof el.closest !== "function") return null;
  if (el.closest("thead, button, input, a")) return null;
  return { row: el.closest<HTMLElement>("tr.row") };
}

export function useMarquee({
  scrollRef,
  navRows,
  selectedPaths,
  selectPaths,
  startMoveDrag,
  enabled = true,
}: {
  scrollRef: React.RefObject<HTMLDivElement>;
  // The rendered row order — the same list the arrow keys walk, so a sweep and
  // a Shift+arrow range can never disagree about what is where.
  navRows: string[];
  // The selection as it stands, which an additive (Shift / Cmd) sweep unions
  // with. Read through a ref so the once-registered pointer handlers see the
  // value at DRAG START rather than the one they closed over.
  selectedPaths: string[];
  selectPaths: (paths: string[]) => void;
  // Hand the press over as a MOVE-DRAG (useRowDrag). The arbiter decides; the
  // drag is somebody else's to run.
  startMoveDrag: (press: {
    path: string;
    pointerId: number;
    clientX: number;
    clientY: number;
  }) => void;
  enabled?: boolean;
}) {
  const rowsRef = useRef<string[]>([]);
  rowsRef.current = navRows;
  const selRef = useRef<string[]>([]);
  selRef.current = selectedPaths;
  const selectRef = useRef(selectPaths);
  selectRef.current = selectPaths;
  const dragRef = useRef(startMoveDrag);
  dragRef.current = startMoveDrag;

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

  // One update of the selection from the pointer's current position. Called from
  // pointermove AND from the auto-scroll frame — scrolling moves the rows under
  // a stationary pointer, which changes the swept set even though the pointer
  // never moved.
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
    const region = marqueeBox(d.origin, at);
    selectRef.current(marqueeHits(region, d.bands, { additive: d.additive, base: d.base }));
  };

  const onPointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!enabled) return;
    // Left button only: the right button opens the background context menu, and
    // the middle one is the browser's.
    if (e.button !== 0) return;
    const pressed = pressedRow(e.target);
    if (!pressed) return;
    const scroller = scrollRef.current;
    if (!scroller) return;
    // THE SNAPSHOT. `selRef` holds the selection from the last RENDER, and this
    // handler runs in the capture phase — before the row's own pointerdown has
    // had a chance to select anything — so this is unambiguously the selection
    // as it stood BEFORE this press. Reading it any later (a `draggable`
    // attribute, a live `.selected` class, the DOM a re-render from now) is the
    // whole of the bug this shape exists to remove.
    //
    // The background presses through the same rule with `rowWasSelected:
    // false` — it has no row to have been selected — so the one function still
    // answers for every pixel, read forwards for the drag and backwards for the
    // sweep.
    const path = pressed.row?.getAttribute(DROP_PATH_ATTR) ?? null;
    if (path !== null && pressStartsDrag({ rowWasSelected: selRef.current.includes(path) })) {
      dragRef.current({
        path,
        pointerId: e.pointerId,
        clientX: e.clientX,
        clientY: e.clientY,
      });
      return;
    }
    // From here down the press is a SWEEP.
    //
    // NO preventDefault here, deliberately. Cancelling a pointerdown's default
    // suppresses the compatibility mouse events that follow it, and this listing
    // has already been bitten once by exactly that (see Listing's
    // collapseNativeSelection: Shift/Cmd-click went silently dead when the click
    // stopped arriving). The job it used to do here — stopping the browser
    // painting a text range as the pointer sweeps — belongs to the scroller's
    // `selectstart` handler, which says no to the selection without saying no to
    // the event.
    //
    // Capturing HERE and not for a move-drag is also why the deferred collapse
    // of a multi-selection still works: capture retargets the pointerup away
    // from the row, and the only presses that reach this line are presses on
    // UNSELECTED rows and on the background — never the press inside a
    // multi-selection whose release the collapse is waiting for.
    //
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
      // Both coordinates, and the whole rect: the pointer being off to the SIDE
      // of the listing stops the scroll, which is the sweep's half of a rule
      // that used to be written out only at the row drag's call site (see
      // autoScrollStep).
      const step = autoScrollStep({ x: d.clientX, y: d.clientY }, r);
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
      if (scroller.hasPointerCapture(ev.pointerId)) scroller.releasePointerCapture(ev.pointerId);
      // Nothing to commit on release: the selection has been live the whole
      // sweep, and the `?sel=` write is already debounced (useListingSelection),
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

  // Named for the phase it MUST be registered in: the snapshot above is only a
  // snapshot because this runs before the row's own pointerdown.
  return { onPointerDownCapture: onPointerDown };
}
