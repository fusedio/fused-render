// The preview pane (right-hand split): the usePreviewPane hook that decides
// whether the pane is there at all and owns the divider drag.
//
// VISIBILITY IS NOT A CHOICE ANY MORE. It used to be: a toggle button, a
// `?preview=true|false` URL param that rode along on directory navigation, and
// a `pane=0` viewstate key so a folder remembered being closed. Three places to
// keep in agreement for one bit, and the bit was almost always a proxy for a
// question the app can answer itself — "is there room for two panes here?".
// So the pane now appears purely from the width of the split container
// (pane-math's shouldShowPane), measured with a ResizeObserver. Measured, not
// read off `window.innerWidth`: the same Listing renders full-window, inside a
// chrome-free embed, and inside another view's split, and only the container
// knows which.
//
// What SURVIVES from the old model is the width, and only the width: `panew`
// (viewstate, per folder) still records a dragged split, because a proportion
// the user chose is a real preference — unlike an on/off the layout can infer.
// Width stays viewstate-only and off the URL: one machine's split isn't
// something a shared link should impose.
//
// Width is a FRACTION of the split container (PANE_DEFAULT_FRAC when nothing
// is saved), rendered as a percentage flex-basis — so the pane keeps its
// proportion when the window resizes, which a resolved pixel width never did.
// Nothing needs measuring for that: a percentage is correct before the first
// paint, whatever the container turns out to be. The pixel floors survive as
// CSS min-widths (.listing-pane-slot / .listing-main) and as the drag's clamp.
// The arithmetic itself is pure and lives in listing/pane-math.ts.
import { useLayoutEffect, useRef, useState } from "react";
import { getViewState, setViewState } from "@platform/lib/viewstate";
import {
  PANE_DEFAULT_FRAC,
  dragPaneFrac,
  parsePaneFrac,
  shouldShowPane,
} from "@apps/explorer/listing/pane-math";

// Merge the pane's width into this folder's saved state without touching a
// saved sort (and vice versa — setSort merges the same way). A null fraction
// (still at the default half) isn't persisted — only a dragged fraction is a
// choice worth remembering.
//
// Three decimals is the whole of the precision a split is worth: it is a
// tenth of a percent of the container, well under a pixel on any window, and
// it keeps the saved string short and readable.
//
// The old `pane` key (the OFF choice) is deleted on the way past rather than
// left alone: folders saved one under the previous model, and a key nothing
// reads is a key that will be misread later.
function savePaneWidth(fsPath: string, frac: number | null): void {
  const s = new URLSearchParams(getViewState(fsPath));
  s.delete("pane");
  if (frac !== null) s.set("panew", String(Math.round(frac * 1000) / 1000));
  else s.delete("panew");
  const qs = s.toString();
  setViewState(fsPath, qs ? "?" + qs : "");
}

// Does the element this ref points at have room for the split? The one place
// the measurement happens, shared by the listing (its split container) and by
// Preview (the body the embed's browse chip pins into, which is the same box).
//
// useLayoutEffect, not useEffect: the first measurement lands BEFORE paint, so
// a wide container never shows one frame of unsplit listing and then jumps.
// The observed element is the container that is always rendered — never the
// pane itself — so showing or hiding the pane cannot feed back into the
// measurement and oscillate.
export function useSplitIsWide(ref: React.RefObject<HTMLElement>): boolean {
  const [wide, setWide] = useState(false);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const read = () => setWide(shouldShowPane(el.getBoundingClientRect().width));
    read();
    const ro = new ResizeObserver(read);
    ro.observe(el);
    return () => ro.disconnect();
  }, [ref]);
  return wide;
}

// `enabled=false` (an embedded Listing — the preview pane's own `_listing`
// mode) turns the whole feature off at the source: however wide that embedded
// listing is, it never grows a pane of its own — no nesting.
export function usePreviewPane(fsPath: string, enabled = true) {
  // `sized` is provenance, not geometry: did the USER choose this fraction
  // (restored from `panew`, or dragged this session), or is it just
  // PANE_DEFAULT_FRAC? Only a chosen fraction is persisted — otherwise the
  // default would be written into `panew` as though it had been dragged.
  // It rides in state rather than a ref so every setPane updater can read it.
  const [pane, setPane] = useState<{ frac: number; sized: boolean }>(() => {
    const saved = enabled
      ? parsePaneFrac(new URLSearchParams(getViewState(fsPath)).get("panew"))
      : null;
    return { frac: saved ?? PANE_DEFAULT_FRAC, sized: saved !== null };
  });
  const splitRef = useRef<HTMLDivElement>(null);
  const wide = useSplitIsWide(splitRef);
  const on = enabled && wide;

  // The divider drag: pointer capture keeps the drag alive when the cursor
  // crosses into the pane's iframe (which would otherwise swallow mousemove).
  const onDividerPointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    e.preventDefault();
    const divider = e.currentTarget;
    divider.setPointerCapture(e.pointerId);
    divider.classList.add("dragging");
    // The pre-drag fraction and provenance, captured once: nothing else can
    // change them while this drag owns the pointer.
    const startSized = pane.sized;
    let frac = pane.frac;
    // Did the drag produce a real fraction? That is what PERSISTENCE reads: in
    // a container narrower than both floors dragPaneFrac returns null (see
    // there), and recording the pre-drag fraction as though the user had chosen
    // it would write a number nobody picked.
    //
    // There used to be a second flag beside it, for the gesture that CLOSED the
    // pane by dragging the divider into the right edge. That gesture is gone
    // with the toggle: closing needs a way back, and with the split decided by
    // width there is no reopen affordance to offer — a pane dragged shut would
    // have stayed shut until the window was resized. The drag now just holds at
    // the pane's floor, which is what the clamp already did all the way to the
    // edge.
    let resized = false;
    const onMove = (ev: PointerEvent) => {
      const rect = splitRef.current?.getBoundingClientRect();
      if (!rect) return;
      // The pane is the right side: its width is the distance from the cursor
      // to the container's right edge, run through the shared FS-12 clamps and
      // divided back into a fraction of the container (dragPaneFrac).
      const next = dragPaneFrac(rect.width, rect.right - ev.clientX);
      if (next === null) return;
      resized = true;
      frac = next;
      setPane((prev) => (prev.frac === frac ? prev : { ...prev, frac }));
    };
    const onUp = () => {
      divider.classList.remove("dragging");
      divider.removeEventListener("pointermove", onMove);
      divider.removeEventListener("pointerup", onUp);
      divider.removeEventListener("pointercancel", onUp);
      // Only a drag that actually RESIZED is a chosen fraction: a bare click on
      // the divider, or a drag in a container too narrow to express a split,
      // both leave the fraction unpersisted.
      const sized = startSized || resized;
      if (resized) setPane((prev) => (prev.sized ? prev : { ...prev, sized: true }));
      savePaneWidth(fsPath, sized ? frac : null);
    };
    divider.addEventListener("pointermove", onMove);
    divider.addEventListener("pointerup", onUp);
    divider.addEventListener("pointercancel", onUp);
  };

  return { pane: { ...pane, on }, splitRef, onDividerPointerDown };
}
