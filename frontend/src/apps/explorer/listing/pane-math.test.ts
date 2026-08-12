// The preview pane's width arithmetic: the split threshold, the undragged
// breakpoints, the clamps, and the drag's px→fraction step. The hook around
// them is React + `location` and belongs to a browser — which is why these live
// in their own router-free module (see pane-math.ts), so this file runs with no
// DOM and in any order.
//
// There is no parse here any more, because there is nothing stored to parse: a
// dragged width lives in memory for the session (pane-store.ts) and no longer
// goes to the per-folder viewstate.
import { describe, expect, test } from "bun:test";
import {
  PANE_DEFAULT_FRAC,
  PANE_MID_W,
  PANE_SPLIT_MIN_W,
  PANE_WIDE_W,
  clampPaneWidth,
  defaultPaneFrac,
  dragPaneFrac,
  shouldShowPane,
} from "./pane-math";

// The split is no longer a toggle — it is a question about how much room the
// listing container has. These are the only cases the DOM wiring can get
// wrong, and they are all here rather than in a layout test, because a
// headless test cannot see layout at all (it can only see this arithmetic).
describe("shouldShowPane", () => {
  test("a roomy container splits", () => {
    expect(shouldShowPane(1440)).toBe(true);
    expect(shouldShowPane(900)).toBe(true);
  });

  test("a cramped container keeps the listing whole", () => {
    expect(shouldShowPane(699)).toBe(false);
    expect(shouldShowPane(420)).toBe(false);
    expect(shouldShowPane(0)).toBe(false);
  });

  test("the threshold itself splits — >=, not >", () => {
    expect(shouldShowPane(PANE_SPLIT_MIN_W)).toBe(true);
    expect(PANE_SPLIT_MIN_W).toBe(700);
  });

  test("an unmeasured container does not split", () => {
    // A width of NaN is what an unattached / display:none element measures as
    // in some engines; guessing "split" there would paint a pane and then rip
    // it away on the first real measurement.
    expect(shouldShowPane(Number.NaN)).toBe(false);
  });

  test("every container that splits can also express a drag", () => {
    // The two thresholds must not disagree: a pane that shows but whose
    // divider drag returns null (dragPaneFrac) would be undraggable.
    expect(dragPaneFrac(PANE_SPLIT_MIN_W, 300)).not.toBeNull();
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

// The undragged split's three steps. Every case here is a width the DOM would
// have to supply, which is exactly why the stepping is arithmetic and not a
// media query: the pane follows its CONTAINER (an embed, another view's split),
// and a media query only ever sees the window.
describe("defaultPaneFrac", () => {
  test("a just-split container gives the listing the room", () => {
    expect(defaultPaneFrac(900)).toBe(0.3);
    expect(defaultPaneFrac(PANE_MID_W - 1)).toBe(0.3);
  });

  test("a normal window splits in half", () => {
    // The middle step IS the default fraction — the one an unmeasured
    // container falls back to, below.
    expect(PANE_DEFAULT_FRAC).toBe(0.5);
    expect(defaultPaneFrac(PANE_MID_W)).toBe(0.5);
    expect(defaultPaneFrac(1280)).toBe(0.5);
    expect(defaultPaneFrac(PANE_WIDE_W - 1)).toBe(0.5);
  });

  test("a wide window gives the preview the room", () => {
    expect(defaultPaneFrac(PANE_WIDE_W)).toBe(0.7);
    expect(defaultPaneFrac(1920)).toBe(0.7);
    expect(defaultPaneFrac(3440)).toBe(0.7);
  });

  test("never proposes a pane narrower than the floor CSS enforces", () => {
    // 30% of the narrowest splitting container is 210px — under the 220px
    // min-width. Left alone, the flex-basis and the min-width would disagree
    // and the divider would sit where the fraction says it doesn't.
    const w = PANE_SPLIT_MIN_W;
    expect(defaultPaneFrac(w) * w).toBeGreaterThanOrEqual(220);
    expect(defaultPaneFrac(w)).toBeGreaterThan(0.3);
  });

  test("every step leaves the listing more than its own floor", () => {
    for (const w of [PANE_SPLIT_MIN_W, 999, PANE_MID_W, 1439, PANE_WIDE_W, 2560]) {
      expect(w - defaultPaneFrac(w) * w).toBeGreaterThan(60);
    }
  });

  test("an unmeasured container answers the middle step", () => {
    // Nothing renders a pane at these widths (shouldShowPane says no); the
    // fraction just must not be NaN or Infinity on the way there.
    expect(defaultPaneFrac(0)).toBe(PANE_DEFAULT_FRAC);
    expect(defaultPaneFrac(Number.NaN)).toBe(PANE_DEFAULT_FRAC);
  });

  test("the steps only ever go up with width", () => {
    let prev = 0;
    for (let w = PANE_SPLIT_MIN_W; w <= 2600; w += 1) {
      const f = defaultPaneFrac(w);
      // Monotonic in the fraction is not required inside the floored region
      // (220/w falls as w grows), but the PANE'S PIXELS must never shrink as
      // the container grows — that is the promise "more room, more preview".
      expect(f * w).toBeGreaterThanOrEqual(prev - 0.001);
      prev = f * w;
    }
  });
});
