// The preview pane's width arithmetic, which is the whole of what is testable
// without a DOM: the clamps, the drag's px→fraction step, and the `panew`
// parse (including its rejection of the legacy pixel values written by the
// pre-fraction model). The hook around them is React + `location` and belongs
// to a browser.
import { describe, expect, test } from "bun:test";
import { PANE_DEFAULT_FRAC, clampPaneWidth, dragPaneFrac, parsePaneFrac } from "./pane";

describe("clampPaneWidth", () => {
  test("passes a comfortable width through untouched", () => {
    expect(clampPaneWidth(1200, 400)).toBe(400);
  });

  test("holds the pane's 220px floor", () => {
    expect(clampPaneWidth(1200, 10)).toBe(220);
  });

  test("leaves the list its 60px sliver", () => {
    expect(clampPaneWidth(1200, 1190)).toBe(1140);
  });

  test("degenerate container: the pane keeps its floor and the list scrolls", () => {
    // 200px of container cannot hold both minimums; PANE_MIN_W is applied last
    // so it is the one that survives.
    expect(clampPaneWidth(200, 190)).toBe(220);
  });
});

describe("dragPaneFrac", () => {
  test("turns the cursor's distance from the right edge into a fraction", () => {
    expect(dragPaneFrac(1000, 300)).toBe(0.3);
  });

  test("the fraction carries the clamp, not the raw pixels", () => {
    // Dragged past the right edge: clamped to 220px, which on a 1000px
    // container is 22%.
    expect(dragPaneFrac(1000, 20)).toBe(0.22);
    // Dragged over the list: clamped to container - 60.
    expect(dragPaneFrac(1000, 990)).toBe(0.94);
  });

  test("an unmeasurable container yields no fraction at all", () => {
    // The caller keeps the fraction it had rather than dividing by zero.
    expect(dragPaneFrac(0, 300)).toBeNull();
    expect(dragPaneFrac(Number.NaN, 300)).toBeNull();
  });
});

describe("parsePaneFrac", () => {
  test("reads a saved fraction", () => {
    expect(parsePaneFrac("0.42")).toBe(0.42);
    expect(parsePaneFrac("1")).toBe(1);
  });

  test("no saved value", () => {
    expect(parsePaneFrac(null)).toBeNull();
    expect(parsePaneFrac("")).toBeNull();
    expect(parsePaneFrac("wide")).toBeNull();
  });

  test("LEGACY PIXEL widths are ignored, never translated", () => {
    // Anything above 1 was written by the pre-fraction model against a
    // container this window may not have. Treated as absent, so the folder
    // opens at the default until it is dragged again.
    expect(parsePaneFrac("420")).toBeNull();
    expect(parsePaneFrac("1140")).toBeNull();
    expect(parsePaneFrac("220")).toBeNull();
  });

  test("nonsense fractions are absent too", () => {
    expect(parsePaneFrac("0")).toBeNull();
    expect(parsePaneFrac("-0.3")).toBeNull();
  });

  test("the default is a half-and-half split", () => {
    expect(PANE_DEFAULT_FRAC).toBe(0.5);
  });
});
