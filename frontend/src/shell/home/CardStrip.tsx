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

// Keep a button press from starting a widget drag in edit mode.
const stop = (e: { stopPropagation: () => void }) => e.stopPropagation();

export function CardStrip({
  count,
  rows,
  total,
  variant = "cards",
  children,
}: {
  variant?: "cards" | "icons";
  count: number | null;
  rows: number;
  total: number;
  children: ReactNode;
}) {
  const scroller = useRef<HTMLDivElement>(null);
  const [edges, setEdges] = useState({ canPrev: false, canNext: false });
  const update = useCallback(() => {
    const el = scroller.current;
    if (!el) return;
    const next = stripEdges(el.scrollLeft, el.clientWidth, el.scrollWidth);
    setEdges((prev) => (prev.canPrev === next.canPrev && prev.canNext === next.canNext ? prev : next));
  }, []);
  useLayoutEffect(update, [update, total, count, rows]);
  useEffect(() => {
    const el = scroller.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(update);
    ro.observe(el);
    return () => ro.disconnect();
  }, [update]);

  const page = (dir: 1 | -1) => {
    const el = scroller.current;
    if (!el) return;
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    el.scrollBy({ left: dir * (el.clientWidth - PEEK_W), behavior: reduce ? "auto" : "smooth" });
  };
  const icons = variant === "icons";
  const overflowing = icons ? total > 0 : count !== null && total > count * rows;
  const { canPrev, canNext } = edges;
  return (
    <div className={"hw-strip" + (canNext ? " has-more" : "")}>
      <div
        ref={scroller}
        className={(icons ? "hw-icons is-strip" : "home-row hw-cards") + (overflowing ? " is-overflowing" : "")}
        style={({ ...(icons ? {} : { "--hw-n": count ?? 1 }), "--hw-rows": rows }) as CSSProperties}
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
