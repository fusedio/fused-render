// THE PATH HALF OF A FOLDER ROW: how much of it fits, and the whole of it on
// hover (Akshil, 2026-09-19 — "let's have start and end of path, rest
// middle-truncate … on hover an instant tooltip / aria-label with the full
// path (avoid z-index issues)").
//
// Two things live in one file because they are one answer: a row shows an
// ABBREVIATED path, so it has to be able to show the unabbreviated one, and
// neither half is worth reading without the other. WHICH characters get cut is
// not here at all — that is `path-fit.ts`, which is pure and therefore testable
// without a browser; this file is only the pixels and the paint.
//
// WHY NOT `title`. The browser holds a `title` back for about a second, by
// which time the pointer has moved on, and it is drawn by the OS: it cannot be
// styled, and on a row inside a modal inside a fixed dropdown it lands wherever
// the platform feels like. One portalled element answers both.
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";
import { fitPathMiddle } from "./path-fit";

// ONE CANVAS FOR THE WHOLE APP. `measureText` is the browser's own text
// measurement — the same engine that will lay the span out — and it costs
// nothing per call, so the alternative (a hidden span per row, measured by
// forcing a layout) would be slower AND less accurate.
//
// `undefined` means "not asked yet", `null` means "asked, and this environment
// has no 2d canvas" — which is every bun test and every jsdom, and is why
// `FitPath` has a full-text fallback rather than a crash.
let measureCtx: CanvasRenderingContext2D | null | undefined;

function measurer(): CanvasRenderingContext2D | null {
  if (measureCtx !== undefined) return measureCtx;
  try {
    measureCtx =
      typeof document === "undefined"
        ? null
        : document.createElement("canvas").getContext("2d");
  } catch {
    measureCtx = null;
  }
  return measureCtx;
}

// A pixel of slack. `measureText` and layout agree to within a rounding error,
// and a string measured at EXACTLY the box's width is the one that wraps or
// clips in the real row.
const SLACK = 1;

/**
 * THE PATH HALF OF A ROW, cut to its own box.
 *
 * `path` is what is SHOWN (already `~`-shortened by the caller); `fullPath` is
 * the absolute one, which is what the accessible name carries, what the tooltip
 * prints and what `data-full` hands a test. The two are deliberately separate:
 * `~/…` is a courtesy to the eye and a lie to anything that would copy it.
 *
 * The font is read once per mount — a row's type does not change under it, and
 * `getComputedStyle` inside a resize callback is the expensive half. Safari
 * sometimes answers `""` for the `font` shorthand, hence the two-part fallback:
 * size and family are the parts `measureText` actually cares about.
 */
/** The width text may occupy in `el`: `clientWidth` less its own padding. */
function contentWidth(el: HTMLElement): number {
  let pad = 0;
  try {
    const cs = getComputedStyle(el);
    pad = (parseFloat(cs.paddingLeft) || 0) + (parseFloat(cs.paddingRight) || 0);
  } catch {
    pad = 0;
  }
  return Math.max(0, el.clientWidth - pad);
}

export function FitPath({
  path,
  fullPath,
  className,
}: {
  path: string;
  fullPath: string;
  className?: string;
}) {
  const ref = useRef<HTMLSpanElement | null>(null);
  const font = useRef("");
  const lastWidth = useRef(-1);
  const [shown, setShown] = useState(path);
  useLayoutEffect(() => {
    const el = ref.current;
    const ctx = measurer();
    // NO CANVAS, NO GUESSING: show the whole path. A test DOM and a browser
    // with canvas disabled both land here, and a long path in a narrow box is
    // simply clipped — which is what the box did before any of this.
    if (!el || !ctx) {
      setShown(path);
      return;
    }
    const fit = () => {
      // THE CONTENT BOX, not `clientWidth`: that includes the span's own padding
      // (`.schedule-recents-where` keeps 8px on the left to stand off the name),
      // and a string measured against it paints 8px wider than the area it is
      // drawn in — with `text-align: right` and `overflow: hidden` the start of
      // the path, the half middle-truncation exists to keep, is what got clipped
      // (Bugbot, PR #1239).
      const box = contentWidth(el);
      lastWidth.current = box;
      // Zero width is a box that has not been laid out yet (or a hidden panel);
      // measuring against it would answer "…" for everything.
      if (!box) {
        setShown(path);
        return;
      }
      if (!font.current) {
        try {
          const cs = getComputedStyle(el);
          font.current = cs.font || `${cs.fontSize} ${cs.fontFamily}`;
        } catch {
          font.current = "";
        }
      }
      ctx.font = font.current;
      setShown(fitPathMiddle(path, (s) => ctx.measureText(s).width <= box - SLACK));
    };
    fit();
    if (typeof ResizeObserver === "undefined") return;
    // Observing the SPAN, not the row: the span is the box the text has to fit
    // inside, and a row can change shape without that box changing at all. The
    // width check is what keeps that honest — the span's own text is what this
    // component is setting, so a callback that re-fit on every notification
    // could chase its own tail on a layout where text does move the box.
    const ro = new ResizeObserver(() => {
      if (el.clientWidth !== lastWidth.current) fit();
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, [path]);
  return (
    <span ref={ref} className={className} aria-label={fullPath} data-full={fullPath}>
      {shown}
    </span>
  );
}

/** Where a tooltip was asked for: the row's own rect, in viewport coordinates.
 *  Kept raw rather than resolved, because where the box GOES depends on how big
 *  it turns out to be, which is not known until it has been painted once. */
type PathTip = { text: string; left: number; top: number; bottom: number };

const TIP_GAP = 6; // between the row and its tooltip
const TIP_EDGE = 8; // between the tooltip and the window
const TIP_HEIGHT_GUESS = 28; // one line of 11px text in its padding

/** The tooltip's corner, clamped into the window. Below the row by default,
 *  above it when the row is near the bottom — and below again when neither side
 *  has room, because a box half off the top is worse than one that overlaps. */
function place(tip: PathTip, width: number, height: number) {
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  const below = tip.bottom + TIP_GAP;
  const above = tip.top - TIP_GAP - height;
  const roomBelow = below + height + TIP_EDGE <= vh;
  return {
    // Left-aligned with the row, never past either edge of the window.
    left: Math.max(TIP_EDGE, Math.min(tip.left, vw - width - TIP_EDGE)),
    top: roomBelow || above < TIP_EDGE ? below : above,
  };
}

/**
 * THE ONE TOOLTIP, portalled to `<body>`.
 *
 * `position: fixed` in the ROOT stacking context is the whole answer to "avoid
 * z-index issues": z-index is only ever a contest between siblings inside one
 * context, so a tooltip rendered inside the modal can never climb out of it,
 * however large a number it is given, and any `overflow` on the way down would
 * clip it besides. Out here there is nothing to climb over.
 *
 * It places itself twice: once from an estimated height, so the first paint is
 * already in the right place, and once from its own measured box, which is what
 * makes a two-line path flip correctly. The second pass is a `useLayoutEffect`,
 * so it lands before the browser paints and nothing is seen to move.
 */
export function PathTipHost({ tip }: { tip: PathTip | null }) {
  const ref = useRef<HTMLDivElement | null>(null);
  const [measured, setMeasured] = useState<{
    tip: PathTip;
    left: number;
    top: number;
  } | null>(null);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!tip || !el) return;
    setMeasured({ tip, ...place(tip, el.offsetWidth, el.offsetHeight) });
  }, [tip]);
  if (!tip || typeof document === "undefined") return null;
  // Identity, not equality: every `show` mints a new object, so a stale
  // placement can never be mistaken for this one's.
  const at =
    measured && measured.tip === tip ? measured : place(tip, 0, TIP_HEIGHT_GUESS);
  return createPortal(
    <div
      ref={ref}
      role="tooltip"
      className="schedule-path-tip"
      style={{
        position: "fixed",
        left: at.left,
        top: at.top,
        zIndex: 10000,
        // Never eat the click meant for the row it is describing.
        pointerEvents: "none",
      }}
    >
      {tip.text}
    </div>,
    document.body,
  );
}

/**
 * THE HOVERED ROW'S WHOLE PATH, instantly.
 *
 * ONE tooltip for a whole list, rather than one per row: the state that matters
 * is "which row is being pointed at", which is a property of the list.
 *
 * The caller wires `show`/`hide` to each ROW — `pointerenter`/`pointerleave`
 * and `focus`/`blur`, so the keyboard ring gets it too — and renders `host`
 * anywhere inside the list; the list closing unmounts the portal with it. No
 * delay: the reader asked for the path by pointing at the row.
 */
export function usePathTip(): {
  show: (el: HTMLElement, text: string) => void;
  hide: () => void;
  host: ReactNode;
} {
  const [tip, setTip] = useState<PathTip | null>(null);
  const hide = useCallback(() => setTip(null), []);
  const show = useCallback((el: HTMLElement, text: string) => {
    const r = el.getBoundingClientRect();
    setTip({ text, left: r.left, top: r.top, bottom: r.bottom });
  }, []);
  const shown = tip !== null;
  useEffect(() => {
    if (!shown) return;
    // A fixed box does not travel with the row it points at, so anything that
    // moves the row takes the tooltip with it. Capture phase, because the
    // scroller that moves it is a DIV inside the modal and its scroll event
    // never reaches the window by bubbling.
    const off = () => setTip(null);
    window.addEventListener("scroll", off, true);
    window.addEventListener("resize", off);
    return () => {
      window.removeEventListener("scroll", off, true);
      window.removeEventListener("resize", off);
    };
  }, [shown]);
  return { show, hide, host: <PathTipHost tip={tip} /> };
}
