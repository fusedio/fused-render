// ---- the wheel works in the margins too -------------------------------------
// `.schedule-main` is capped at 1050px and centred, so a wide window leaves a
// band of empty page either side of the content — and the content is the
// scroller, so a wheel out in that band lands on `.schedule-page`, which is
// `overflow: hidden` and scrolls nothing: the reader had to aim at the card
// (Akshil, 2026-08-20 for the List; 2026-09-05 again for the Cards wall).
//
// Making the scroller full-width instead was tried, twice, and traded this for
// worse bugs each time — on the List the card's frame scrolled away with its
// content and the reserved scrollbar gutters pushed the card off the toolbar's
// axis (bugbot, #678); on the Cards wall the bar landed at the window edge while
// the List's sat at the column's, so the two views wore different scrollbars
// (Akshil, 2026-09-05). So the geometry stays exactly as it is and the wheel is
// forwarded: a wheel anywhere on the page that no scroller of its own claims is
// handed to the view's scroller.
//
// ONE hook for every view that scrolls in that column, so the List and the
// Cards wall cannot drift into two forwarding rules. Board and Calendar mount
// their own components and do not call it, so nothing here touches theirs.
import { useEffect } from "react";
import type { RefObject } from "react";

/** Forward wheel events that land on the page's margins to `ref`'s element.
 *  Mount once per view; the ref is read per wheel, so it may still be null on
 *  the mount this runs after (rows in flight) and simply does nothing then. */
export function useMarginWheel(ref: RefObject<HTMLElement | null>): void {
  useEffect(() => {
    // The page div, NOT via the ref: the page ancestor is the one element of
    // this pair that is always there when the effect runs.
    const page = document.querySelector<HTMLElement>(".schedule-page");
    if (!page) return;
    const onWheel = (e: WheelEvent) => {
      const el = ref.current;
      if (!el || !(e.target instanceof Element) || e.deltaY === 0) return;
      // Pinch-zoom arrives as ctrl+wheel; that is a zoom, not a scroll.
      if (e.ctrlKey) return;
      // Anything between the pointer and the page that scrolls for itself —
      // the scroller, a menu, a popover — keeps its native wheel untouched.
      for (let n: Element | null = e.target; n && n !== page; n = n.parentElement) {
        const s = getComputedStyle(n);
        if (
          (s.overflowY === "auto" || s.overflowY === "scroll") &&
          n.scrollHeight > n.clientHeight
        ) {
          return;
        }
      }
      // deltaMode 1 is lines (Firefox with a wheel mouse); everything else
      // that reaches a web page is already pixels.
      el.scrollTop += e.deltaMode === 1 ? e.deltaY * 16 : e.deltaY;
    };
    page.addEventListener("wheel", onWheel, { passive: true });
    return () => page.removeEventListener("wheel", onWheel);
  }, [ref]);
}
