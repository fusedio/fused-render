// The scrollable row a capability's cards live in, and its own "there is more"
// affordance.
//
// A CAROUSEL, not a wrapping grid, and that is the Local tab's layout decision
// rather than a flourish. A capability's row is open-ended — the disk half grows
// with every download and the recommended half is as long as the curation is —
// and a grid that wrapped turned each capability into a block of unknown height,
// so the THIRD heading was reliably below the fold on a page whose whole job is
// to be swept. One row per capability keeps every heading on screen at once and
// puts the length inside the row, where a horizontal scroll is the reader's own
// business.
//
// Its own file, beside the two cards it holds, because it is the one piece of
// this tab that is pure DOM measurement: no listing, no job rows, no props but
// its children.
import { useEffect, useRef, useState, type ReactNode } from "react";

/** The scrollable row, with its own "there is more" affordance.
 *
 *  Exact thirds took away the old signal — a card half in view — so the row has
 *  to SAY it scrolls. Each arrow exists only while there is content on its side
 *  (measured overflow, never a width threshold), floats over the row's edge so
 *  appearing and disappearing move no layout, and a click slides by one card so
 *  the reader keeps their place. The re-measure runs on scroll, on resize and
 *  on every render — a finished download replaces a card and changes
 *  `scrollWidth` without any resize firing.
 */
export function Carousel({ children }: { children: ReactNode }) {
  const rowRef = useRef<HTMLDivElement | null>(null);
  const [canPrev, setCanPrev] = useState(false);
  const [canNext, setCanNext] = useState(false);

  const measure = () => {
    const row = rowRef.current;
    if (!row) return;
    // 1px slack: a fractional card width leaves scrollLeft short of the true
    // end, and an arrow that never disables reads as a broken button.
    setCanPrev(row.scrollLeft > 1);
    setCanNext(row.scrollLeft + row.clientWidth < row.scrollWidth - 1);
  };

  useEffect(() => {
    const row = rowRef.current;
    if (!row) return;
    row.addEventListener("scroll", measure, { passive: true });
    const ro = new ResizeObserver(measure);
    ro.observe(row);
    return () => {
      row.removeEventListener("scroll", measure);
      ro.disconnect();
    };
  }, []);
  useEffect(measure);

  const step = (dir: 1 | -1) => {
    const row = rowRef.current;
    if (!row) return;
    const card = row.firstElementChild as HTMLElement | null;
    const gap = parseFloat(getComputedStyle(row).columnGap) || 0;
    row.scrollBy({
      left: dir * ((card ? card.offsetWidth : row.clientWidth) + gap),
      behavior: "smooth",
    });
  };

  return (
    <div className="am-carousel-wrap">
      {canPrev && (
        <button
          type="button"
          className="am-caro-btn prev"
          aria-label="Scroll back"
          onClick={() => step(-1)}
        >
          ‹
        </button>
      )}
      <div className="am-carousel" ref={rowRef}>
        {children}
      </div>
      {canNext && (
        <button
          type="button"
          className="am-caro-btn next"
          aria-label="Scroll forward"
          onClick={() => step(1)}
        >
          ›
        </button>
      )}
    </div>
  );
}
