// THE CONTROL STRIP'S WORDS FIT OR FOLD — T:7554-7595 `annFitStrip`, the same
// collision detection `fit.ts` does for the composer row one pane over.
//
// COLLISION-DETECTED, NEVER A BREAKPOINT (Akshil, 2026-08-19: a fixed width
// dropped the words while there was still space for them). Measure with the
// words ON — take `.tight` off, ask what the row needs, put it back only if the
// content truly would not fit — so widening undoes itself with no state to get
// stale. The stylesheet does the rest through the `--c-annlbl` token every
// `.c-lbl` span already reads.
//
// This is what QA #2 was missing: at 1280px with the pane taking most of the
// width the chat column is ~308px, and `Screenshot · Comment · Annotate` at full
// labels measured 308.3px with its right edge 8px past the viewport — legacy has
// rendered the same row icon-only since 2026-08-19.
//
// TWO OBSERVERS, ONE VERDICT, and the split is the load-bearing part:
//   * ResizeObserver on the row — the box changes (the divider drags, the window
//     resizes, a narrow layout flips).
//   * MutationObserver on the row's CHILDREN — the content changes width with
//     the box standing still (the picker slides in, a seat swaps faces, a label
//     grows). Deliberately NOT on the row itself for attributes: this function
//     writes the row's own class list, and observing the node it writes would
//     make every verdict schedule the next one, forever (T:7551). A second
//     childList-only observer on the row re-seats the child observers when React
//     adds or removes one.
import { useCallback, useRef } from "react";

import { measureRowNeed } from "./fit";

/** T:7566 — `.tight` on or off, recomputed from scratch. `need` is injected so a
 *  test can prove the verdict without a layout engine. */
export function fitStrip(
  row: HTMLElement | null | undefined,
  need: (row: HTMLElement) => number = measureRowNeed,
): void {
  if (!row) return;
  // NOT LAID OUT YET IS NOT A VERDICT. The first `run()` fires from the ref
  // callback during commit, when the row can still measure `clientWidth: 0` —
  // and any `need > 0` against that stamps `.tight`, which the ResizeObserver's
  // first real delivery then takes back off, so the strip painted icon-only for
  // one frame on every mount. A row with no width has no answer to give, so the
  // class is left exactly as it is (unmeasurable is not "it fits") and the
  // observers deliver the first verdict that means anything.
  if (!row.clientWidth) return;
  // MEASURED WITH THE WORDS ON. Both halves run synchronously inside an
  // observer callback, which fires before paint, so the probe never shows.
  row.classList.remove("tight");
  if (need(row) > row.clientWidth) row.classList.add("tight");
}

const KID_OPTS: MutationObserverInit = {
  subtree: true,
  childList: true,
  characterData: true,
  attributes: true,
  attributeFilter: ["class", "hidden"],
};

/**
 * A ref CALLBACK rather than a ref plus an effect, because the row is
 * conditionally rendered (`stripShown`) and the observers have to follow it in
 * and out rather than be wired once against a node that may not be there.
 */
export function useFitStrip(): (row: HTMLElement | null) => void {
  const off = useRef<(() => void) | null>(null);
  return useCallback((row: HTMLElement | null) => {
    off.current?.();
    off.current = null;
    if (!row) return;
    const run = (): void => fitStrip(row);
    const ro = typeof ResizeObserver === "function" ? new ResizeObserver(run) : null;
    ro?.observe(row);
    const kids = typeof MutationObserver === "function" ? new MutationObserver(run) : null;
    const seat = (): void => {
      kids?.disconnect();
      for (const kid of Array.from(row.children)) kids?.observe(kid, KID_OPTS);
    };
    const self =
      typeof MutationObserver === "function"
        ? new MutationObserver(() => {
            seat();
            run();
          })
        : null;
    // childList ONLY: the row's own attributes are what this writes.
    self?.observe(row, { childList: true });
    seat();
    run();
    off.current = () => {
      ro?.disconnect();
      kids?.disconnect();
      self?.disconnect();
    };
  }, []);
}
