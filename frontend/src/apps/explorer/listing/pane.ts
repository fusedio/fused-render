// The preview pane (right-hand split): the usePreviewPane hook that owns the
// pane's width and its divider drag.
//
// **NO CONDITIONAL LAYOUT LOGIC LIVES HERE ANY MORE** (D282, the owner's "remove
// any complicated breakpoint logic"). Two generations of that are gone. First a
// user-facing on/off — a toggle button, a `?preview=true|false` URL param that
// rode along on directory navigation, and a `pane=0` viewstate key so a folder
// remembered being closed: three places to keep in agreement for one bit. Then the
// thing that replaced it, a **700px width gate** on the split container measured
// with a `ResizeObserver`, which decided whether there was a pane at all, plus
// 30/50/70 width tiers stepping on two more breakpoints.
//
// Now: the pane is there whenever this Listing is one that has a pane (the
// caller's `enabled` — not embedded, not a snapshot, not a panel pane), and it
// takes 30% of its container, the same share the file view's sidebar takes. There
// is no measurement, no threshold and no tier; a narrow window gets a narrow
// listing beside a floored pane, and `_side=off` is the way out of it, exactly as
// on a wide one.
//
// What SURVIVES from the old model is the width, and only the width: a dragged
// split is a real preference, unlike an on/off the layout can infer. But it is
// no longer remembered PER FOLDER, and no longer stored at all. It used to be a
// `panew` key in the per-path viewstate map, which meant the divider jumped on
// ordinary navigation — out of a folder you had dragged, into one you had not,
// and the pane snapped between your width and the default. There is
// now one width for the session, in memory, in pane-store.ts (which is where
// the reasoning about that lives, including why a REFRESH deliberately clears
// it). Off the URL for the same reason as before: one machine's split isn't
// something a shared link should impose.
//
// Width is a FRACTION of the split container, rendered as a percentage
// flex-basis — so a pane keeps its proportion when the window resizes, which a
// resolved pixel width never did. UNDRAGGED it is `PANE_DEFAULT_FRAC`, a constant.
// The pixel floors survive as CSS min-widths (.listing-pane-slot /
// .listing-main) and as the drag's clamp; those are clamps, not breakpoints. The
// arithmetic itself is pure and lives in listing/pane-math.ts.
import { useLayoutEffect, useRef, useState } from "react";
import { purgeViewStateParams } from "@platform/lib/viewstate";
import { getPaneFrac, setPaneFrac } from "@apps/explorer/listing/pane-store";
import { companionFrac, dragPaneFrac } from "@apps/explorer/listing/pane-math";

// THE ONE-TIME PURGE of the per-folder width, run at module init — which is the
// first time anything in the app cares about a pane at all, and the only place
// that ever wrote these keys.
//
// Both are gone for good:
//   `panew`  the per-folder fraction, whose per-folder-ness was the bug (see
//            the header and pane-store.ts). Left in storage it would do nothing
//            except wait to be misread by a later reader.
//   `pane`   the OFF choice from the model before that, which the old
//            savePaneWidth deleted opportunistically on its way past — i.e.
//            only for folders the user happened to drag again. This clears the
//            rest.
// Every user therefore starts on the adaptive default and keeps it until their
// next drag; nothing here can be translated into the new model, because a width
// chosen for one folder is not a statement about the session.
//
// Sorts are NOT touched: `?sort`/`&order` stay per folder on purpose (two
// sibling folders keep independent sorts), which is exactly why the purge names
// its params instead of clearing the map.
purgeViewStateParams("panew", "pane");

// The split container's measured width — back with D283, for ONE question: is this
// container small (`companionFrac`, 720px and under → the companion takes half
// instead of a third). Measured on the CONTAINER and never read off
// `window.innerWidth`, because the same Listing renders full-window, inside a
// chrome-free embed and inside another view's split, and only the container knows
// which — an embedded pane in a small frame is small.
//
// `useLayoutEffect`, so the first measurement lands before paint and a wide
// container never shows one frame at half width and then jumps. The observed
// element is the container that is always rendered — never the pane itself — so the
// pane's own width cannot feed back into the measurement and oscillate.
//
// *D282 deleted this, and it deleted `useSplitIsWide` with it — the 700px verdict
// that decided whether there was a pane at all, plus the 30/50/70 tiers. Neither
// comes back: what returns is the number, read for one boolean.*
export function useSplitWidth(ref: React.RefObject<HTMLElement>): number {
  const [w, setW] = useState(0);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const read = () => {
      const next = el.getBoundingClientRect().width;
      setW((prev) => (prev === next ? prev : next));
    };
    read();
    const ro = new ResizeObserver(read);
    ro.observe(el);
    return () => ro.disconnect();
  }, [ref]);
  return w;
}

// `enabled=false` (an embedded Listing — the preview pane's own `_listing`
// mode) turns the whole feature off at the source: however wide that embedded
// listing is, it never grows a pane of its own — no nesting.
export function usePreviewPane(enabled = true) {
  // The fraction the USER chose — dragged somewhere in this session, on this
  // folder or another (pane-store). `null` is not a missing number but a real
  // state, "no choice yet", and it is still worth distinguishing now that the
  // alternative is a constant: `setPaneFrac` must record a width only when a drag
  // produced one, so that a refresh returns everyone to the plain 30% rather than
  // to a number that was never chosen.
  //
  // Seeded from the store rather than mirrored from it: the store is the source
  // of truth ACROSS mounts (this hook remounts on every navigation and reads it
  // again), while within a mount the React state is what re-renders. Nothing
  // else writes the store, so the two cannot drift.
  const [chosen, setChosen] = useState<number | null>(getPaneFrac);
  // Still a ref, and still the split container: the DRAG reads its rect directly
  // (below) to turn a cursor position into a fraction. What went is the standing
  // measurement of it.
  const splitRef = useRef<HTMLDivElement>(null);
  // `on` is exactly the caller's own question — is this a Listing that has a pane at
  // all — and no width enters into it. That is the half D282 settled and D283 does
  // not reopen: the measurement below decides the pane's SHARE, never its existence.
  const on = enabled;
  const width = useSplitWidth(splitRef);
  const frac = chosen ?? companionFrac(width);

  // The divider drag: pointer capture keeps the drag alive when the cursor
  // crosses into the pane's iframe (which would otherwise swallow mousemove).
  const onDividerPointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    e.preventDefault();
    const divider = e.currentTarget;
    divider.setPointerCapture(e.pointerId);
    divider.classList.add("dragging");
    // The pre-drag fraction, captured once: nothing else can change it while
    // this drag owns the pointer. It is the RENDERED one, so a drag that starts
    // from the undragged default continues from where the divider actually is
    // rather than jumping.
    let dragged = frac;
    // Did the drag produce a real fraction? That is what the COMMIT below
    // reads: in a container narrower than both floors dragPaneFrac returns null
    // (see there), and recording the pre-drag fraction as though the user had
    // chosen it would keep a number nobody picked.
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
      dragged = next;
      // The first move is already a choice: from here the pane leaves the shared
      // default and renders what the cursor says.
      setChosen((prev) => (prev === next ? prev : next));
    };
    const onUp = () => {
      divider.classList.remove("dragging");
      divider.removeEventListener("pointermove", onMove);
      divider.removeEventListener("pointerup", onUp);
      divider.removeEventListener("pointercancel", onUp);
      // Only a drag that actually RESIZED records anything. A bare click on the
      // divider, or a drag in a container too narrow to express a split (see
      // dragPaneFrac), leaves the pane where it was — following the window if
      // it was already following it, keeping the session's width if there is
      // one. A pane that is still FOLLOWING the window must stay that way: any
      // write here turns it into a chosen width, everywhere, for the rest of
      // the session.
      //
      // Three decimals is the whole of the precision a split is worth: a tenth
      // of a percent of the container, well under a pixel on any window.
      if (!resized) return;
      const settled = Math.round(dragged * 1000) / 1000;
      setPaneFrac(settled);
      // Render the rounded number too, so what is on screen now and what the
      // next folder opens at are the same value rather than differing by the
      // rounding.
      setChosen(settled);
    };
    divider.addEventListener("pointermove", onMove);
    divider.addEventListener("pointerup", onUp);
    divider.addEventListener("pointercancel", onUp);
  };

  return { pane: { frac, on }, splitRef, onDividerPointerDown };
}
