// The composer chrome's measured fitting — the pure half of T:12157-12497
// (`composerRowNeed` / `fitComposerRow` / `fitSelect` / `fitFootnote` /
// `fitHomeTitle`). MEASURED, never a breakpoint: the template detects
// collisions and declares no widths, because three of the row's seats carry
// text the user changes (03 §G).
//
// Everything that can be arithmetic is arithmetic and lives above the DOM
// helpers, so `fit.test.ts` can prove the ladder without a browser.

/** The row's four states, in the order the width is spent (T:12203-12228). */
export type RowFit = "full" | "compact" | "tight" | "stack";

/** One laid-out child of the control row, as the sum needs it (T:12262). */
export interface Seat {
  /** `getBoundingClientRect().width` — FRACTIONAL: the browser's own wrap
   *  decision is made in sub-pixels and offsetWidth rounds (T:12253-12261). */
  width: number;
  marginLeft: number;
  marginRight: number;
  /** The flex spacer: a seat (so it is charged a gap on each side) whose width
   *  is slack rather than content (T:12240-12250). */
  spacer?: boolean;
  /** `display: none` — not laid out, so no width AND no gap (T:12246). */
  hidden?: boolean;
}

export interface RowBox {
  paddingLeft: number;
  paddingRight: number;
  columnGap: number;
}

/** What the row needs on ONE line. NOT scrollWidth: on a wrapping flex row it
 *  is floored at clientWidth, so an overflowing row reports no overflow at all
 *  — the exact failure that made this invisible to CSS (T:12235-12239). */
export function rowNeed(box: RowBox, seats: readonly Seat[]): number {
  let need = box.paddingLeft + box.paddingRight;
  let count = 0;
  for (const seat of seats) {
    if (seat.hidden) continue;
    count += 1;
    if (seat.spacer) continue;
    need += seat.width + seat.marginLeft + seat.marginRight;
  }
  if (count > 1) need += box.columnGap * (count - 1);
  return need;
}

/** The ladder, recomputed from scratch: classes off, ask the row what it needs,
 *  put back only what is warranted — so widening undoes itself with no state to
 *  get stale (T:12327-12362). `need` is asked for each candidate in turn and
 *  must apply that candidate before measuring. */
export function pickRowFit(box: number, need: (fit: RowFit) => number): RowFit {
  if (need("full") <= box) return "full";
  if (need("compact") <= box) return "compact";
  if (need("tight") <= box) return "tight";
  return "stack";
}

/** Which classes a verdict carries. The ladder is cumulative: `.stack` is a
 *  tight compact row that has also folded (T:12345-12351). */
export function fitFlags(fit: RowFit): {
  compact: boolean;
  tight: boolean;
  stack: boolean;
} {
  return {
    compact: fit !== "full",
    tight: fit === "tight" || fit === "stack",
    stack: fit === "stack",
  };
}

/** The footnote's budget is TWO LINES, and the second sentence is what pays
 *  (T:12369-12380). Measured in lines because the sentence's own length is the
 *  other variable — it names the target's kind. */
export function footnoteTight(
  clientHeight: number,
  paddingTop: number,
  paddingBottom: number,
  lineHeight: number,
): boolean {
  const text = clientHeight - paddingTop - paddingBottom;
  return lineHeight > 0 && text > lineHeight * 2 + 1;
}

/** The landing title's sizes, and they are the STYLESHEET's — this only picks
 *  the largest one the name fits on (T:12411). */
export const HOME_TITLE_STEPS: readonly (readonly [number, string])[] = [
  [26, ""],
  [21, "c-t-mid"],
  [17, "c-t-min"],
];

/** Ratios, not a re-measure per step: text width scales with font-size, so one
 *  canvas pass prices all three candidates. `""` = the full 26px fits; nothing
 *  fitting on one line takes the smallest step and WRAPS (T:12430-12446). */
export function pickHomeTitleStep(
  need: number,
  base: number,
  box: number,
): string {
  const px = base || HOME_TITLE_STEPS[0][0];
  for (const [size, cls] of HOME_TITLE_STEPS) {
    if ((need * size) / px <= box) return cls;
  }
  return HOME_TITLE_STEPS[HOME_TITLE_STEPS.length - 1][1];
}

// ---- DOM readers -----------------------------------------------------------

let ctx: CanvasRenderingContext2D | null | undefined;

/** The one way this module prices a single line of text: off-DOM, at the
 *  element's own computed font. `null` in a DOM-less test (T:12157). */
function textCtx(): CanvasRenderingContext2D | null {
  if (ctx === undefined) {
    ctx =
      typeof document === "undefined"
        ? null
        : document.createElement("canvas").getContext("2d");
  }
  return ctx ?? null;
}

/** Width of `text` set in `el`'s font. Canvas ignores `letter-spacing`, so a
 *  negatively tracked headline comes out a hair WIDE — an error in the
 *  direction of shrinking one step early, the harmless direction (T:12417). */
export function measureTextIn(el: Element, text: string): number {
  const c = textCtx();
  if (!c) return 0;
  const s = getComputedStyle(el);
  c.font = `${s.fontStyle} ${s.fontWeight} ${s.fontSize} ${s.fontFamily}`;
  return c.measureText(text).width;
}

/** Read one row's seats off the DOM. `display: none` is asked of the computed
 *  style rather than inferred from a zero width, which cannot tell "not laid
 *  out" from "laid out at zero" (T:12246). */
export function readRow(row: HTMLElement): { box: RowBox; seats: Seat[] } {
  const cs = getComputedStyle(row);
  const seats: Seat[] = [];
  for (const child of Array.from(row.children)) {
    const s = getComputedStyle(child);
    const width = child.getBoundingClientRect().width;
    // A CHILD THAT HOLDS NO SEAT, on T's own test: `if (!c.offsetWidth)
    // continue; // a hidden child holds no seat` (T:7570-7576). A zero-width
    // child pays neither its width nor A GAP — and the gap was the difference,
    // since `display: none` was the only case skipped here, so a laid-out
    // zero-width child was still charged one and the fold verdict came out a
    // sub-pixel different at the boundary.
    //
    // `display: none` stays an explicit test rather than being inferred from
    // the zero: it is the honest read of "not laid out" (T:12246), and the two
    // together cover both ways a child can be nothing.
    if (s.display === "none" || width === 0) {
      seats.push({ width: 0, marginLeft: 0, marginRight: 0, hidden: true });
      continue;
    }
    seats.push({
      width,
      marginLeft: parseFloat(s.marginLeft) || 0,
      marginRight: parseFloat(s.marginRight) || 0,
      spacer: child.classList.contains("c-spacer"),
    });
  }
  return {
    box: {
      paddingLeft: parseFloat(cs.paddingLeft) || 0,
      paddingRight: parseFloat(cs.paddingRight) || 0,
      columnGap: parseFloat(cs.columnGap) || 0,
    },
    seats,
  };
}

/** `composerRowNeed(row)` (T:12262). */
export function measureRowNeed(row: HTMLElement): number {
  const { box, seats } = readRow(row);
  return rowNeed(box, seats);
}

/** An appearance:none select keeps the width of its WIDEST option, so a short
 *  value renders in a wide pill with dead space before the next pill. Hug the
 *  selected label instead. Skipped when the value is not in the options —
 *  keep the last fitted width rather than blanking the pill (T:12158-12170). */
export function fitSelect(el: HTMLSelectElement): void {
  const opt = el.selectedOptions[0];
  if (!opt) return;
  const s = getComputedStyle(el);
  const chrome =
    (parseFloat(s.paddingLeft) || 0) +
    (parseFloat(s.paddingRight) || 0) +
    (parseFloat(s.borderLeftWidth) || 0) +
    (parseFloat(s.borderRightWidth) || 0);
  el.style.width = `${Math.ceil(measureTextIn(el, opt.textContent || "") + chrome)}px`;
}

/** Line two's right-hand anchor, decided here because the stylesheet cannot
 *  name it: the first control after the spacer (T:12354-12362). Cleared on
 *  every pass — an anchor left on a control since hidden pins the wrong thing. */
export function applyLead2(row: HTMLElement, stacked: boolean): void {
  let lead: Element | null = null;
  if (stacked) {
    let past = false;
    for (const child of Array.from(row.children)) {
      if (child.classList.contains("c-spacer")) {
        past = true;
        continue;
      }
      if (past && (child as HTMLElement).offsetWidth) {
        lead = child;
        break;
      }
    }
  }
  for (const child of Array.from(row.children))
    child.classList.toggle("c-lead2", child === lead);
}
