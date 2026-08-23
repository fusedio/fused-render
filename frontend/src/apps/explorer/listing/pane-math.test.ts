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
// dragged width lives in memory for the session — shared with the file
// sidebar since D443 (`lib/side-store.ts`), in pixels, and no longer in a
// fraction of its own (`pane-store.ts`, deleted) or in the per-folder
// viewstate before that.
import { describe, expect, test } from "bun:test";
import { COMPANION_FRAC, companionFrac } from "@apps/explorer/lib/side-width";
import {
  MAX_PANE_SHARE,
  PANE_DEFAULT_FRAC,
  clampPaneWidth,
  clampSharedPaneWidth,
  dragPaneFrac,
  paneDragCloses,
  paneFracFromSharedWidth,
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

// The floor-last clamp both dragPaneFrac and paneFracFromSharedWidth read
// through — the fix for the second review pass's two findings: a local drag
// and an imported width must agree with what renders (MUST FIX), and the
// share cap must never win over the pane's own pixel floor (FIX).
describe("clampSharedPaneWidth", () => {
  test("a comfortable width passes through untouched", () => {
    expect(clampSharedPaneWidth(1200, 400)).toBe(400);
  });

  test("the pixel floor holds below the share cap", () => {
    expect(clampSharedPaneWidth(1200, 10)).toBe(220);
  });

  test("the share cap holds where the pixel floor alone would not", () => {
    // The list-floor ceiling here is 1140 (1200 - 60 = 95%); the share cap
    // catches it first at 840 (70%).
    expect(clampSharedPaneWidth(1200, 1190)).toBe(840);
  });

  test("the pixel floor wins over the share cap when they disagree", () => {
    // At 280px the share cap alone would ask for 196px (70% of 280) — below
    // the 220px floor. The floor is applied LAST and wins outright: this is
    // the exact bug the second review pass caught (a computed fraction below
    // the floor disagreeing with CSS's own `min-width: 220px`).
    expect(clampSharedPaneWidth(280, 900)).toBe(220);
    expect(clampSharedPaneWidth(280, 900)).toBeGreaterThan(280 * MAX_PANE_SHARE);
  });

  test("degenerate container: the pane keeps its floor and the list scrolls", () => {
    expect(clampSharedPaneWidth(200, 190)).toBe(220);
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
    // Dragged over the list: the pixel floor alone would clamp to
    // container - 60 (94%), but MAX_PANE_SHARE catches it first at 70% — a
    // LOCAL drag is bounded exactly like an imported width now (second
    // review pass: rendering and the stored commit must agree, so the same
    // clamp has to answer both).
    expect(dragPaneFrac(1000, 990)).toBe(MAX_PANE_SHARE);
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

  test("the fraction a real drag produces can never reach 1, or exceed MAX_PANE_SHARE once the container is wide enough", () => {
    // Below ~314px (PANE_MIN_W / MAX_PANE_SHARE) the pane's own floor asks
    // for a bigger share than the cap allows, and floor-last means the
    // floor wins — so 300px is still governed by the pixel floor alone.
    expect(dragPaneFrac(300, 600)).toBeCloseTo(220 / 300, 10);
    // From there up, MAX_PANE_SHARE is the ceiling a real drag can reach —
    // never the old (W - 60) / W, which would have let a 1024px container
    // reach ~94%.
    for (const w of [640, 1024, 1920, 3840]) {
      const widest = dragPaneFrac(w, w * 2) as number;
      expect(widest).toBeLessThan(1);
      expect(widest).toBe(MAX_PANE_SHARE);
    }
  });

  test("an unmeasurable container yields no fraction at all", () => {
    // The caller keeps the fraction it had rather than dividing by zero.
    expect(dragPaneFrac(0, 300)).toBeNull();
    expect(dragPaneFrac(Number.NaN, 300)).toBeNull();
  });
});

// -------------------------------------------------------- the shared-width seam
// D443: the pane's stored width is the SAME pixel number the file sidebar
// drags (`lib/side-store.ts`), re-clamped into this pane's own (narrower)
// floors on every read rather than the file sidebar's.
describe("paneFracFromSharedWidth", () => {
  test("nothing dragged yet (in either surface) is the plain companion share", () => {
    expect(paneFracFromSharedWidth(null, 1200)).toBe(companionFrac(1200));
    expect(paneFracFromSharedWidth(null, 900)).toBe(companionFrac(900));
  });

  test("a comfortable shared width converts straight through", () => {
    expect(paneFracFromSharedWidth(400, 1200)).toBeCloseTo(400 / 1200, 10);
  });

  test("a width dragged wide on the FILE sidebar is still re-clamped here", () => {
    // The file sidebar's own floor is 380px, comfortably inside this pane's
    // range too, so an ordinary file-sidebar drag needs no clamping — the
    // point is that it CAN be, not that this case triggers it.
    expect(paneFracFromSharedWidth(600, 1200)).toBeCloseTo(600 / 1200, 10);
  });

  test("a shared width narrower than either surface's own floor is clamped up", () => {
    // 100px is below both this pane's 220px floor and the file sidebar's
    // 380px one, so no ordinary drag on either surface produces it — the
    // clamp still has to hold for whatever arrives.
    expect(paneFracFromSharedWidth(100, 1200)).toBeCloseTo(220 / 1200, 10);
  });

  test("a shared width wider than this container's list floor allows is clamped down", () => {
    // 1190/1200 would be 95% by the pixel floor alone (list at its 60px
    // sliver) — MAX_PANE_SHARE catches it first.
    expect(paneFracFromSharedWidth(1190, 1200)).toBe(MAX_PANE_SHARE);
  });

  test("an unmeasured container answers the companion share, not a division by zero", () => {
    expect(paneFracFromSharedWidth(400, 0)).toBe(companionFrac(0));
    expect(paneFracFromSharedWidth(400, Number.NaN)).toBe(companionFrac(Number.NaN));
  });

  // -------------------------------------------------- the imported-width ceiling
  // Two real failure modes once the pixel number can arrive from elsewhere
  // (D443's own follow-up): a width dragged wide on the FILE SIDEBAR of a
  // much bigger monitor, and this container merely SHRINKING under a width
  // that no longer moves with it (the whole point of storing pixels rather
  // than a proportion — see pane-math.ts's header).

  test("a width dragged wide on a much bigger monitor's file sidebar cannot open this listing at a sliver", () => {
    // The file sidebar has no share cap of its own — only pixel floors — so
    // a 3840px-wide monitor's sidebar can be dragged out past 2000px. Read
    // back on an ordinary 1200px folder window, the pixel floor alone would
    // clamp it to 1140 (95%); the share ceiling holds it at 70% instead.
    expect(paneFracFromSharedWidth(2200, 1200)).toBe(MAX_PANE_SHARE);
  });

  test("a window shrinking under an already-dragged pixel width is capped the same way", () => {
    // Drag to 900px while the container is 1400px wide (64%, comfortably
    // under the cap) — nothing capped yet.
    expect(paneFracFromSharedWidth(900, 1400)).toBeCloseTo(900 / 1400, 10);
    // The SAME 900px, read back after the window shrinks to 1000px, would be
    // 90% by the pixel floor alone (FS-12's own regression case: "the listing
    // collapses") — the share ceiling holds it at 70%.
    expect(paneFracFromSharedWidth(900, 1000)).toBe(MAX_PANE_SHARE);
  });

  test("a degenerate container (< 280px) ignores the shared width entirely", () => {
    // Below PANE_MIN_W + LIST_MIN_W, clampSharedPaneWidth returns PANE_MIN_W
    // (220) regardless of input — more pixels than the container has — and
    // dividing it out would answer a fraction over 1 (`flexBasis: "110%"`),
    // which dragPaneFrac itself refuses to produce (it answers null there).
    // This module has no null to hand back, so it falls back to the plain
    // companion share instead, unconditionally, before the shared width is
    // even read.
    expect(paneFracFromSharedWidth(900, 200)).toBe(companionFrac(200));
    expect(paneFracFromSharedWidth(900, 279)).toBe(companionFrac(279));
    expect(paneFracFromSharedWidth(900, 200)).toBeLessThanOrEqual(1);
  });

  test("280px is still the narrowest container the shared width can reach — and the FLOOR wins there, not the share cap", () => {
    // Both floors fit exactly (PANE_MIN_W=220 of 280 = ~78.6%), so unlike the
    // degenerate case above the shared width IS honoured and clamped. But
    // 78.6% is ABOVE MAX_PANE_SHARE (70%) — a share cap alone would ask for
    // 196px here, below the pane's own 220px floor, and CSS's
    // `min-width: 220px` would then override the computed flex-basis. The
    // floor is applied LAST specifically to avoid that: it wins outright in
    // this narrow band (up to ~314px, PANE_MIN_W / MAX_PANE_SHARE), exactly
    // as it did before the cap existed.
    expect(paneFracFromSharedWidth(900, 280)).toBeCloseTo(220 / 280, 10);
  });

  test("MAX_PANE_SHARE only becomes the ceiling once the container is wide enough that it exceeds the floor", () => {
    // Just past ~314px (PANE_MIN_W / MAX_PANE_SHARE = 314.28...), the share
    // cap asks for more pixels than the floor does, and the cap takes over.
    expect(paneFracFromSharedWidth(900, 320)).toBe(MAX_PANE_SHARE);
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
