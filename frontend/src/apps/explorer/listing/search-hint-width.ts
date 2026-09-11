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
