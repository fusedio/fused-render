import { expect, test } from "bun:test";
import {
  fitFlags,
  footnoteTight,
  HOME_TITLE_STEPS,
  pickHomeTitleStep,
  pickRowFit,
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
