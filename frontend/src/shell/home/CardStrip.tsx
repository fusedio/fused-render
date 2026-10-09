// A cards widget's row as a horizontally scrolling strip: the next card peeks in
// at the right edge (so it reads as scrollable) and ‹ › buttons page it. The
// card width comes from CSS (.hw-cards), driven by --hw-n / --hw-rows.
import { useCallback, useEffect, useLayoutEffect, useRef, useState, type CSSProperties, type ReactNode } from "react";
import Chevron from "@platform/ui/Chevron";
import { PEEK_W } from "./strip";

/** Which scroll directions have more to show; 1px tolerance for subpixel scroll. */
export function stripEdges(
  scrollLeft: number,
  clientWidth: number,
  scrollWidth: number,
): { canPrev: boolean; canNext: boolean } {
  return {
    canPrev: scrollLeft > 1,
    canNext: scrollLeft + clientWidth < scrollWidth - 1,
  };
}

/** Within one viewport of the end (also true when the content does not overflow). */
export function nearEnd(scrollLeft: number, clientWidth: number, scrollWidth: number): boolean {
  return scrollLeft + clientWidth >= scrollWidth - clientWidth;
}

/** Gap between icon tiles (.hw-icons). */
const ICON_GAP = 8;
/** Icon tile width (.hw-icons.is-strip grid-auto-columns). */
const ICON_TILE_W = 76;
/** One icon tile: 8+8 padding, 48 icon, 4 gap, ~14 label line (.hw-tile). */
const ICON_TILE_H = 82;

/** How many icon rows fit in `height`, never fewer than `minRows`. */
export function iconRowsThatFit(height: number, tileH: number, gap: number, minRows: number): number {
  if (!(height > 0) || !(tileH > 0)) return minRows;
  return Math.max(minRows, Math.floor((height + gap) / (tileH + gap)));
}

/** How many whole icon columns fit in `width`, never fewer than one. */
export function iconColsThatFit(width: number, tileW: number, gap: number): number {
  if (!(width > 0) || !(tileW > 0)) return 1;
  return Math.max(1, Math.floor((width + gap) / (tileW + gap)));
}

/** Pixel width of `cols` whole icon columns, so no partial column peeks in. */
export function iconColsWidth(cols: number, tileW: number, gap: number): number {
  return cols * tileW + (cols - 1) * gap;
}

/**
 * `fitRows` capped so a few items do not stack into one column: with column
 * flow, `itemCount` items need only ceil(itemCount / colsThatFit) rows.
 */
export function cappedIconRows(fitRows: number, itemCount: number, colsThatFit: number): number {
  const cols = Math.max(1, colsThatFit);
  return Math.min(fitRows, Math.max(1, Math.ceil(itemCount / cols)));
}

// Keep a button press from starting a widget drag in edit mode.
const stop = (e: { stopPropagation: () => void }) => e.stopPropagation();

export function CardStrip({
  count,
  rows,
  total,
  variant = "cards",
  onNearEnd,
  children,
}: {
  /** Called whenever the strip is within one viewport of its end (scroll, resize, new content). */
  onNearEnd?: () => void;
  variant?: "cards" | "icons";
  count: number | null;
  rows: number;
  total: number;
  children: ReactNode;
}) {
  const scroller = useRef<HTMLDivElement>(null);
  const [edges, setEdges] = useState({ canPrev: false, canNext: false });
  const [fitRows, setFitRows] = useState(0);
  const [fitCols, setFitCols] = useState(0);
  const nearEndRef = useRef(onNearEnd);
  nearEndRef.current = onNearEnd;
  const update = useCallback(() => {
    const el = scroller.current;
    if (!el) return;
    if (nearEnd(el.scrollLeft, el.clientWidth, el.scrollWidth)) nearEndRef.current?.();
    if (variant === "icons") {
      const tileH = (el.firstElementChild as HTMLElement | null)?.offsetHeight || ICON_TILE_H;
      const tileW = (el.firstElementChild as HTMLElement | null)?.offsetWidth || ICON_TILE_W;
      // The scroller is itself sized to whole columns, so the room is its parent's width.
      const room = el.parentElement?.clientWidth || el.clientWidth;
      const colsThatFit = iconColsThatFit(room, tileW, ICON_GAP);
      setFitCols((prev) => (prev === colsThatFit ? prev : colsThatFit));
      const n = cappedIconRows(iconRowsThatFit(el.clientHeight, tileH, ICON_GAP, 1), el.childElementCount, colsThatFit);
      setFitRows((prev) => (prev === n ? prev : n));
    }
    const next = stripEdges(el.scrollLeft, el.clientWidth, el.scrollWidth);
    setEdges((prev) => (prev.canPrev === next.canPrev && prev.canNext === next.canNext ? prev : next));
  }, [variant]);
  useLayoutEffect(update, [update, total, count, rows]);
  useEffect(() => {
    const el = scroller.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(update);
    ro.observe(el);
    if (variant === "icons" && el.parentElement) ro.observe(el.parentElement);
    return () => ro.disconnect();
  }, [update, variant]);

  const page = (dir: 1 | -1) => {
    const el = scroller.current;
    if (!el) return;
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    // Icons page by whole columns (the scroller is exactly that wide); cards leave a peek.
    const step = variant === "icons" ? el.clientWidth + ICON_GAP : el.clientWidth - PEEK_W;
    el.scrollBy({ left: dir * step, behavior: reduce ? "auto" : "smooth" });
  };
  const icons = variant === "icons";
  const iconsW = icons && fitCols > 0 ? iconColsWidth(fitCols, ICON_TILE_W, ICON_GAP) : undefined;
  const overflowing = icons ? total > 0 : count !== null && total > count * rows;
  const { canPrev, canNext } = edges;
  return (
    <div className={"hw-strip" + (canNext && !icons ? " has-more" : "")}>
      <div
        ref={scroller}
        className={(icons ? "hw-icons is-strip" : "home-row hw-cards") + (overflowing ? " is-overflowing" : "")}
        style={(({ ...(icons ? { width: iconsW } : { "--hw-n": count ?? 1 }), "--hw-rows": icons ? Math.max(rows, fitRows) : rows }) as unknown) as CSSProperties}
        onScroll={update}
      >
        {children}
      </div>
      {canPrev ? (
        <button
          type="button"
          className="hw-strip-nav is-prev"
          aria-label="Scroll left"
          onMouseDown={stop}
          onDragStart={stop}
          onClick={() => page(-1)}
        >
          <Chevron dir="left" />
        </button>
      ) : null}
      {canNext ? (
        <button
          type="button"
          className="hw-strip-nav is-next"
          aria-label="Scroll right"
          onMouseDown={stop}
          onDragStart={stop}
          onClick={() => page(1)}
        >
          <Chevron dir="right" />
        </button>
      ) : null}
    </div>
  );
}
