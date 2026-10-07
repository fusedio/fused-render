// Per-widget row sizing: how many 330px cards fit across a measured element.
// Moved verbatim from Home.tsx when the strips became widgets; each cards
// widget now measures its own body rather than one shared column.
import { useCallback, useRef, useState } from "react";
import { navigateUrl } from "@platform/lib/router";

// One row per section: the page measures its own width and renders exactly
// as many full-size cards as fit — no wrapping, no clipping, no scrolling.
// The full lists live behind "See all".
// Card width + gap must match the .home-row CSS.
export const CARD_W = 330;
export const CARD_GAP = 16;
// The ceiling on what a section may fetch/keep — enough for a very wide
// window, and the same cap the two endpoints apply to `limit` themselves
// (HOME_APPS_LIMIT / HOME_SESSION_LIMIT). NOT the number either one asks for:
// see useStripCount.
export const MAX_ROW = 12;

// How many cards fit across the sections' shared container right now, plus the
// most that have ever fit — the number the fetches ask for.
// One ResizeObserver on the wrapper, one count for all three strips.
//
// The two numbers are not the same, and the difference is load-bearing. Asking
// for MAX_ROW when the row draws three cards is what made the server's
// recents-first fast path unreachable: /api/apps/home skips its exhaustive
// workspace walk only once the recents FILL the request (routers/apps.py), so a
// request for twelve walked the whole workspace on every visit for anyone with
// fewer than twelve opened apps — which is nearly everyone. A row that asks for
// what it can draw puts that walk back to being the fallback it is documented
// as, and the session row's per-directory transcript reads (its endpoint stops
// as soon as `limit` folders land) shrink with it.
//
// `count` is null until the wrapper has actually been MEASURED, and both
// fetches wait for it. A guess would be a request for cards the row cannot
// show, which is the same bug in smaller print. Nothing flashes for it: a
// callback ref runs in the commit phase, so the measured value is in before the
// browser paints.
//
// `limit` is the PEAK count, never the current one — widening the window needs
// cards the first fetch did not ask for, while narrowing it already holds
// enough, so a drag that shrinks the row refetches nothing.
//
// A CALLBACK ref, not useRef+useEffect: the measured wrapper UNMOUNTS while a
// search is live (`searching ? null : <div ref=…>`), and a mount-once effect
// only ever saw the first element — on unmount the observer fired against the
// detached node (clientWidth 0 → count 1) and the remounted wrapper was never
// observed again, so clearing a search left every strip at one card per row.
// The callback re-runs on each mount/unmount: it tears the old observer down
// and measures the element actually on screen.
export function useStripCount() {
  // One state, not two: `limit` is derived from the same measurement as
  // `count`, and splitting them would let a render see a count the limit had
  // not accounted for yet.
  const [size, setSize] = useState<{ count: number | null; limit: number | null }>({
    count: null,
    limit: null,
  });
  const roRef = useRef<ResizeObserver | null>(null);
  const ref = useCallback((el: HTMLDivElement | null) => {
    roRef.current?.disconnect();
    roRef.current = null;
    if (!el) return;
    const measure = () => {
      const fits = Math.max(
        1,
        Math.floor((el.clientWidth + CARD_GAP) / (CARD_W + CARD_GAP)),
      );
      // Same object back when the count is unchanged: a ResizeObserver fires
      // for every pixel of a window drag, and only a changed card count is a
      // reason to re-render (or, via `limit`, to fetch).
      setSize((prev) =>
        prev.count === fits
          ? prev
          : { count: fits, limit: Math.max(prev.limit ?? 0, fits) },
      );
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    roRef.current = ro;
  }, []);
  return { ref, count: size.count, limit: size.limit };
}

export function softNavigate(e: React.MouseEvent, href: string) {
  if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
  e.preventDefault();
  navigateUrl(href);
}

