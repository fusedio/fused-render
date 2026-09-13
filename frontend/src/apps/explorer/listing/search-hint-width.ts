// A callback ref, not an object ref plus a mount-only effect — because the
// element it watches does not stay put. The search box this ref attaches to
// (Listing.tsx's `searchBoxRef`) gets portaled into the crumb bar once a
// folder claims it (search-slot.ts), and a portal swap REBUILDS the subtree:
// the inline node is torn down and a new one is created inside the portal
// target (see node-slot.ts's own comment on exactly this failure mode — "a
// reference captured at mount would then point at a detached div"). An
// object ref read once inside a `useLayoutEffect(..., [])` measures whichever
// node existed at that first commit and never looks again: if the swap
// happens moments later, the observer stays attached to a node that has
// already left the document, and the width it reports freezes forever.
//
// A callback ref has no such blind spot — React calls it with `null` on
// every unmount and with the fresh node on every mount, portal swaps
// included, so re-attaching here on each call keeps the measurement current
// no matter how many times the underlying node is rebuilt.
import { useCallback, useRef } from "react";

/**
 * Returns a ref callback that watches an element's `clientWidth` against
 * `thresholdPx`, calling `onChange` with the current wide/narrow verdict
 * immediately on attach and again on every resize — and re-measures from
 * scratch each time the ref is handed a new element.
 */
export function useWidthThresholdRef(
  thresholdPx: number,
  onChange: (wide: boolean) => void,
): (el: HTMLElement | null) => void {
  const roRef = useRef<{ disconnect(): void } | null>(null);

  return useCallback(
    (el: HTMLElement | null) => {
      roRef.current?.disconnect();
      roRef.current = null;
      if (!el) return;
      const measure = () => onChange(el.clientWidth >= thresholdPx);
      measure();
      const ro = new ResizeObserver(measure);
      ro.observe(el);
      roRef.current = ro;
    },
    [thresholdPx, onChange],
  );
}

// THE OMNIBOX-OVERLAP-DEFECT FOLLOW-UP (running-screen review, 2026-09-13):
// the trailing "Search ⌘L" button's reservation against the crumbs strip
// used to be a hand-estimated pixel count in explorer.css, sized generously
// for the widest label form ("Ctrl L") so it would never be too tight. That
// generosity is exactly the new defect — a Mac user's shorter "⌘L" label (or
// the collapsed glyph) leaves the estimate reserving far more than the
// button actually occupies, and the crumbs give up that dead space for
// nothing, truncating a long path to a leading ellipsis with visible air
// between its tail and the button. An estimate cannot be right on both
// platforms, both label forms, and every font/zoom setting at once — only
// measuring the button's OWN rendered position can be.
//
// `useControlReservationRef` measures the gap a "control" element (here, the
// shortcut button) actually occupies against a "box" element's right edge —
// `box.right - control.left`, i.e. exactly the CSS `right` value the button
// is already positioned with (8px unscoped, 38px past the star in the
// crumb-slot host) PLUS the button's own current rendered width. Reading it
// out of the DOM this way, rather than duplicating those 8px/38px insets
// here, means a future change to either host's inset needs no matching edit
// in this file: the geometry is the single source of truth, this hook only
// reports it.
//
// Two elements, not one ResizeObserver on the control alone: the control's
// `right: Npx` anchor keeps `box.right - control.left` constant across a BOX
// resize for a fixed N (the button's left edge moves exactly as much as the
// box's right edge does), so a box-only resize needs no recompute — but the
// control's own SIZE changes (the label/glyph swap `boxWide` drives, a
// platform's longer/shorter label, a font or zoom change) do, and only
// observing the control catches exactly that. Both elements are still
// tracked (not just observed) because the delta is measured fresh off both
// rects every time either ref (re)attaches — see below for why identity
// changes, not just resizes, have to trigger a recompute too.
//
// Callback refs on BOTH elements, same reasoning as `useWidthThresholdRef`
// above: the box this attaches to is `SearchField.tsx`'s
// `.listing-search-box`, which this same component already documents as
// getting torn down and rebuilt on a portal swap into the crumb bar
// (search-slot.ts) — an object ref plus a mount-only effect would freeze on
// whichever node existed at first commit and never notice the swap. The
// control is the shortcut button itself, which additionally mounts and
// unmounts on its own schedule entirely independent of any portal (it only
// renders while `!pinnedOpen && !hasClear`) — a callback ref is the only
// thing that observes both kinds of churn uniformly.
//
// `onChange(null)` — not a stale number — is reported the moment either
// element is missing: on first paint before both refs have attached, on the
// button's own unmount (focus, or a query typed in), and on the box's
// unmount during a portal swap before the new box re-attaches. A caller that
// left the LAST measured value in place here would reserve room for a
// button that is no longer there, silently truncating the path for no
// reason — the exact failure mode this whole mechanism exists to avoid, just
// moved from "wrong estimate" to "stale measurement."
export function useControlReservationRef(
  gapPx: number,
  onChange: (reservationPx: number | null) => void,
): {
  boxRef: (el: HTMLElement | null) => void;
  controlRef: (el: HTMLElement | null) => void;
} {
  const boxElRef = useRef<HTMLElement | null>(null);
  const controlElRef = useRef<HTMLElement | null>(null);
  const roRef = useRef<{ disconnect(): void } | null>(null);

  const measure = useCallback(() => {
    const box = boxElRef.current;
    const control = controlElRef.current;
    if (!box || !control) {
      onChange(null);
      return;
    }
    onChange(box.getBoundingClientRect().right - control.getBoundingClientRect().left + gapPx);
  }, [gapPx, onChange]);

  // Re-run on every (re)attach of EITHER ref, not just on resize — a portal
  // swap or the button's own mount/unmount changes which nodes exist without
  // necessarily firing a ResizeObserver callback on either (the new box node
  // can be the same size as the old one), so identity changes need their own
  // trigger, separate from size changes.
  const attach = useCallback(() => {
    roRef.current?.disconnect();
    roRef.current = null;
    const box = boxElRef.current;
    const control = controlElRef.current;
    if (!box || !control) {
      onChange(null);
      return;
    }
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(control);
    roRef.current = ro;
  }, [measure, onChange]);

  const boxRef = useCallback(
    (el: HTMLElement | null) => {
      boxElRef.current = el;
      attach();
    },
    [attach],
  );
  const controlRef = useCallback(
    (el: HTMLElement | null) => {
      controlElRef.current = el;
      attach();
    },
    [attach],
  );

  return { boxRef, controlRef };
}
