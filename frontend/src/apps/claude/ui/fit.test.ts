import { expect, test } from "bun:test";
import {
  fitFlags,
  footnoteTight,
  HOME_TITLE_STEPS,
  pickHomeTitleStep,
  pickRowFit,
  readRow,
  rowNeed,
  type RowFit,
  type Seat,
} from "./fit";

const pill = (width: number): Seat => ({
  width,
  marginLeft: 0,
  marginRight: 0,
});
const spacer: Seat = { width: 0, marginLeft: 0, marginRight: 0, spacer: true };
const box = { paddingLeft: 0, paddingRight: 0, columnGap: 6 };

test("rowNeed charges the spacer a SEAT but no width (T:12240)", () => {
  // Three 40px pills + the spacer + send: five seats, four gaps.
  const seats = [pill(40), pill(40), pill(40), spacer, pill(32)];
  expect(rowNeed(box, seats)).toBe(40 * 3 + 32 + 6 * 4);
});

test("rowNeed skips a display:none child entirely — no width and no gap", () => {
  const hidden: Seat = {
    width: 0,
    marginLeft: 0,
    marginRight: 0,
    hidden: true,
  };
  expect(rowNeed(box, [pill(40), hidden, pill(40)])).toBe(80 + 6);
  expect(rowNeed(box, [pill(40), pill(40)])).toBe(80 + 6);
});

test("rowNeed counts padding and margins, sub-pixel", () => {
  const seats = [{ width: 40.5, marginLeft: 1.25, marginRight: 0.25 }];
  expect(
    rowNeed({ paddingLeft: 2.5, paddingRight: 1.5, columnGap: 6 }, seats),
  ).toBeCloseTo(46, 5);
});

test("rowNeed charges no gap for a single seat", () => {
  expect(rowNeed(box, [pill(40)])).toBe(40);
});

test("the ladder is full -> compact -> tight -> stack and stops at the first fit", () => {
  const needs: Record<RowFit, number> = {
    full: 420,
    compact: 400,
    tight: 370,
    stack: 200,
  };
  const asked: RowFit[] = [];
  const probe = (fit: RowFit) => {
    asked.push(fit);
    return needs[fit];
  };
  expect(pickRowFit(440, probe)).toBe("full");
  expect(asked).toEqual(["full"]);

  asked.length = 0;
  expect(pickRowFit(410, probe)).toBe("compact");
  expect(asked).toEqual(["full", "compact"]);

  asked.length = 0;
  expect(pickRowFit(380, probe)).toBe("tight");
  expect(asked).toEqual(["full", "compact", "tight"]);

  asked.length = 0;
  // Below even the dense line: stack is the last resort and is NOT re-measured.
  expect(pickRowFit(300, probe)).toBe("stack");
  expect(asked).toEqual(["full", "compact", "tight"]);
});

test("a need exactly equal to the box fits — the browser wraps only past it", () => {
  expect(pickRowFit(400, () => 400)).toBe("full");
  expect(pickRowFit(399, () => 400)).not.toBe("full");
});

test("fitFlags is cumulative: stack is a tight compact row that folded", () => {
  expect(fitFlags("full")).toEqual({
    compact: false,
    tight: false,
    stack: false,
  });
  expect(fitFlags("compact")).toEqual({
    compact: true,
    tight: false,
    stack: false,
  });
  expect(fitFlags("tight")).toEqual({
    compact: true,
    tight: true,
    stack: false,
  });
  expect(fitFlags("stack")).toEqual({
    compact: true,
    tight: true,
    stack: true,
  });
});

test("footnote goes tight past two lines plus a pixel (T:12377)", () => {
  // 16.5px lines, 2 lines of text: inside the budget.
  expect(footnoteTight(33, 0, 0, 16.5)).toBe(false);
  expect(footnoteTight(34, 0, 0, 16.5)).toBe(false); // == 2*lh + 1, still in
  expect(footnoteTight(35, 0, 0, 16.5)).toBe(true);
  // Padding is not text and is taken off first.
  expect(footnoteTight(43, 6, 4, 16.5)).toBe(false);
  expect(footnoteTight(45, 6, 4, 16.5)).toBe(true);
  // No line-height to divide by: never tight.
  expect(footnoteTight(99, 0, 0, 0)).toBe(false);
});

test("the home title picks the largest step the name fits on (T:12430)", () => {
  const base = 26;
  // A short name fits at full size.
  expect(pickHomeTitleStep(200, base, 400)).toBe("");
  // 400px of text in a 340px column: 26 -> 21 (400*21/26 = 323).
  expect(pickHomeTitleStep(400, base, 340)).toBe("c-t-mid");
  // Narrower still: only the 17px step prices in.
  expect(pickHomeTitleStep(400, base, 270)).toBe("c-t-min");
  // Nothing fits on one line: the smallest step, wrapping.
  expect(pickHomeTitleStep(1200, base, 200)).toBe("c-t-min");
  expect(HOME_TITLE_STEPS.map(([px]) => px)).toEqual([26, 21, 17]);
});

test("a zero base font falls back to the first step's size", () => {
  expect(pickHomeTitleStep(26, 0, 26)).toBe("");
});

// ---- readRow: which children hold a seat (T:7570-7576) --------------------

/** A row and its children, as `readRow` reads them: `getComputedStyle` and
 *  `getBoundingClientRect`. Small enough to build by hand, and the alternative
 *  is no coverage of `readRow` at all — this suite has no DOM. */
function fakeRow(
  kids: Array<{ display?: string; width: number; ml?: number; mr?: number; cls?: string }>,
  rowBox: { columnGap?: number; paddingLeft?: number; paddingRight?: number } = {},
) {
  const styles = new Map<unknown, Record<string, string>>();
  const row = {
    children: kids.map((k) => {
      const el = {
        getBoundingClientRect: () => ({ width: k.width }),
        classList: { contains: (c: string) => c === k.cls },
      };
      styles.set(el, {
        display: k.display ?? "flex",
        marginLeft: String(k.ml ?? 0) + "px",
        marginRight: String(k.mr ?? 0) + "px",
      });
      return el;
    }),
  };
  styles.set(row, {
    columnGap: String(rowBox.columnGap ?? 6) + "px",
    paddingLeft: String(rowBox.paddingLeft ?? 0) + "px",
    paddingRight: String(rowBox.paddingRight ?? 0) + "px",
  });

  const G = globalThis as Record<string, unknown>;
  const real = G.getComputedStyle;
  G.getComputedStyle = (el: unknown) => styles.get(el) ?? {};
  try {
    return readRow(row as unknown as HTMLElement);
  } finally {
    if (real === undefined) delete G.getComputedStyle;
    else G.getComputedStyle = real;
  }
}

test("a LAID-OUT zero-width child holds no seat, so it pays no gap (T:7570-7576)", () => {
  // T's own test is `if (!c.offsetWidth) continue; // a hidden child holds no
  // seat`. Native skipped only `display: none`, so a zero-width child that WAS
  // laid out still cost a gap and the fold verdict came out a sub-pixel
  // different right at the boundary.
  const { box: b, seats } = fakeRow([{ width: 40 }, { width: 0 }, { width: 40 }]);
  expect(seats[1]!.hidden).toBe(true);
  expect(seats[1]!.width).toBe(0);
  // Two seats, ONE gap — not three seats and two gaps.
  expect(rowNeed(b, seats)).toBe(80 + 6);
});

test("`display: none` is still its own test, not inferred from the width", () => {
  // It is the honest read of "not laid out" (T:12246), and a `none` child can
  // report a non-zero rect in some engines — so the two together cover both
  // ways a child can be nothing.
  const { box: b, seats } = fakeRow([
    { width: 40 },
    { width: 33, display: "none" },
    { width: 40 },
  ]);
  expect(seats[1]!.hidden).toBe(true);
  expect(seats[1]!.width).toBe(0);
  expect(rowNeed(b, seats)).toBe(80 + 6);
});

test("a real child keeps its fractional width and its margins", () => {
  // The fractional `getBoundingClientRect().width` stays — it is the more
  // correct read, and `fit.ts` documents why.
  const { seats } = fakeRow([{ width: 40.5, ml: 2, mr: 3 }]);
  expect(seats[0]!.width).toBe(40.5);
  expect(seats[0]!.marginLeft).toBe(2);
  expect(seats[0]!.marginRight).toBe(3);
  expect(seats[0]!.hidden).toBeUndefined();
});

test("the spacer is still recognised by its class", () => {
  const { seats } = fakeRow([{ width: 0, cls: "c-spacer" }]);
  // Zero-width AND a spacer: `hidden` wins, which is the same answer either
  // way — a spacer charges a seat but no width, and a hidden child charges
  // neither. The row it is in has other seats to space.
  expect(seats[0]!.hidden).toBe(true);
});
