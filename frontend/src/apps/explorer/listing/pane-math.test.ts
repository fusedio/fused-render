// The preview pane's width arithmetic: ONE fraction, the drag's clamps, and the
// drag's px→fraction step. The hook around them is React + `location` and belongs
// to a browser — which is why these live in their own router-free module (see
// pane-math.ts), so this file runs with no DOM and in any order.
//
// Two things this file used to test and no longer can, both DELETED with D282:
// the 700px split threshold (`shouldShowPane`) and the undragged 30/50/70 tiers
// (`defaultPaneFrac`). The pane is one plain 30% now, the same share the file
// view's sidebar takes, and nothing about its width or its presence is decided by
// how wide anything is.
//
// There is no parse here either, because there is nothing stored to parse: a
// dragged width lives in memory for the session (pane-store.ts) and no longer
// goes to the per-folder viewstate.
import { describe, expect, test } from "bun:test";
import { COMPANION_FRAC } from "@apps/explorer/lib/side-width";
import {
  PANE_DEFAULT_FRAC,
  clampPaneWidth,
  dragPaneFrac,
  paneDragCloses,
} from "./pane-math";
// The whole module, to assert what it no longer offers.
import * as paneMath from "./pane-math";

// ONE RULE FOR BOTH COMPANION COLUMNS (D282, the owner's "they are the same
// concept now"). The two surfaces are a folder's preview pane and a file's
// sidebar; the rule is shared rather than spelled twice, because two literals
// are how they drifted to 50% and 30% in the first place.
describe("the pane's width", () => {
  test("its general share is 30%, and IS the file sidebar's", () => {
    expect(PANE_DEFAULT_FRAC).toBe(0.3);
    expect(PANE_DEFAULT_FRAC).toBe(COMPANION_FRAC);
  });

  test("the TIER ladder stays deleted", () => {
    // D283 restored ONE small-container step (`companionFrac`, 50% at 720px and
    // under), so the module does take a width again — what must not come back is
    // the 30/50/70 ladder on 1000px/1440px, its constants, and the 700px
    // visibility gate. Those are named individually for that reason; a two-value
    // step reusing any of these identifiers would read as the ladder returning.
    const exported = Object.keys(paneMath);
    expect(exported).not.toContain("defaultPaneFrac");
    expect(exported).not.toContain("shouldShowPane");
    expect(exported).not.toContain("PANE_SPLIT_MIN_W");
    expect(exported).not.toContain("PANE_MID_W");
    expect(exported).not.toContain("PANE_WIDE_W");
  });
});

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

  test("a container too narrow for both floors expresses no split at all", () => {
    // Under 280px (220 + 60) the clamp returns PANE_MIN_W whatever the cursor
    // does, so any fraction it yielded would describe the CONTAINER, not a
    // choice — at 220px exactly 1.0, "the pane takes everything", which no
    // wider window can honour. One drag in a narrow pane used to persist that
    // and leave the list a 60px sliver forever after.
    expect(dragPaneFrac(220, 170)).toBeNull();
    expect(dragPaneFrac(220, 300)).toBeNull();
    expect(dragPaneFrac(279, 100)).toBeNull();
  });

  test("280px is the narrowest container that still means something", () => {
    // Both floors fit exactly, so the split is decided even though it has only
    // one possible value.
    expect(dragPaneFrac(280, 500)).toBeCloseTo(220 / 280, 10);
    expect(dragPaneFrac(280, 0)).toBeCloseTo(220 / 280, 10);
  });

  test("the fraction a real drag produces can never reach 1", () => {
    // The ceiling is (W - LIST_MIN_W) / W, which is below 1 for every width.
    for (const w of [300, 640, 1024, 1920, 3840]) {
      const widest = dragPaneFrac(w, w * 2) as number;
      expect(widest).toBeLessThan(1);
      expect(widest).toBeCloseTo((w - 60) / w, 10);
    }
  });

  test("an unmeasurable container yields no fraction at all", () => {
    // The caller keeps the fraction it had rather than dividing by zero.
    expect(dragPaneFrac(0, 300)).toBeNull();
    expect(dragPaneFrac(Number.NaN, 300)).toBeNull();
  });
});

// ---------------------------------------------------------------- drag close
// The listing pane's version of the sidebars' drag-to-close (#680): between
// the 220px floor and half of it the clamp renders the resistance band, and
// only a pull clean through — the cursor within 110px of the right edge —
// reads as "shut it".
describe("paneDragCloses", () => {
  test("a drag through the resistance band closes", () => {
    expect(paneDragCloses(1000, 109)).toBe(true);
    expect(paneDragCloses(1000, 0)).toBe(true);
    expect(paneDragCloses(1000, -50)).toBe(true);
  });

  test("holding inside the band, or above the floor, does not", () => {
    expect(paneDragCloses(1000, 110)).toBe(false); // the band's own edge sticks
    expect(paneDragCloses(1000, 219)).toBe(false);
    expect(paneDragCloses(1000, 500)).toBe(false);
  });

  test("a container too narrow to express a split never closes by drag", () => {
    // dragPaneFrac is null there — the pane holds its floor whatever the
    // cursor does, so there is no band whose crossing could mean anything.
    expect(paneDragCloses(279, 0)).toBe(false);
    expect(paneDragCloses(0, 0)).toBe(false);
    expect(paneDragCloses(Number.NaN, 0)).toBe(false);
  });
});
