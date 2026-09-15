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
// sidebar since D460 (`lib/side-store.ts`), in pixels, and no longer in a
// fraction of its own (`pane-store.ts`, deleted) or in the per-folder
// viewstate before that.
import { describe, expect, test } from "bun:test";
import { COMPANION_FRAC, companionFrac } from "@apps/explorer/lib/side-width";
import { closeOverdrag } from "@platform/lib/panel-drag";
import { SIDE_PANE_MIN_WIDTH } from "@platform/lib/pane-metrics";

/** The list's own sliver, the other half of every "both floors together" below.
 *  Not exported by pane-math (it is an implementation detail of the clamps), so
 *  it is restated here — the tests that use it all derive from the PANE's floor,
 *  which is the one that moves. */
const LIST_MIN_W = 60;
import {
  MAX_PANE_SHARE,
  PANE_DEFAULT_FRAC,
  PANE_MIN_W,
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

describe("the pane's floor", () => {
  test("is the SHARED one, not a copy of it", () => {
    // The bug this pins (code review, batch 3): `PANE_MIN_W` was its own literal
    // 220 here, and when `.listing-pane-slot`'s CSS floor moved to the composer
    // row's width every clamp below went on computing against a number the
    // layout would not render — so between the two floors the divider walked
    // away from the cursor, for every user, on every drag.
    expect(PANE_MIN_W).toBe(SIDE_PANE_MIN_WIDTH);
  });
});

describe("clampPaneWidth", () => {
  test("passes a comfortable width through untouched", () => {
    expect(clampPaneWidth(1200, 400)).toBe(400);
  });

  test("holds the pane's own floor, whatever the shared constant says it is", () => {
    // Not a literal: the floor moved once already (220 → the composer row's
    // width) and this module read a stale copy of it for the whole of that
    // change. Asking the constant is what a test can do that a number cannot.
    expect(clampPaneWidth(1200, 10)).toBe(SIDE_PANE_MIN_WIDTH);
  });

  test("leaves the list its 60px sliver", () => {
    expect(clampPaneWidth(1200, 1190)).toBe(1140);
  });

  test("degenerate container: the pane keeps its floor and the list scrolls", () => {
    // 200px of container cannot hold both minimums; PANE_MIN_W is applied last
    // so it is the one that survives.
    expect(clampPaneWidth(200, 190)).toBe(SIDE_PANE_MIN_WIDTH);
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
    expect(clampSharedPaneWidth(1200, 10)).toBe(SIDE_PANE_MIN_WIDTH);
  });

  test("the share cap holds where the pixel floor alone would not", () => {
    // The list-floor ceiling here is 1140 (1200 - 60 = 95%); the share cap
    // catches it first at 840 (70%).
    expect(clampSharedPaneWidth(1200, 1190)).toBe(840);
  });

  test("the pixel floor wins over the share cap when they disagree", () => {
    // Just above both floors together the share cap alone asks for 70% of the
    // container, which is fewer pixels than the pane's own floor — 322 of a
    // 460px container. The floor is applied LAST and wins outright: this is
    // the exact bug the second review pass caught (a computed fraction below
    // the floor disagreeing with CSS's own `min-width`).
    const narrow = SIDE_PANE_MIN_WIDTH + LIST_MIN_W;
    expect(clampSharedPaneWidth(narrow, 900)).toBe(SIDE_PANE_MIN_WIDTH);
    expect(clampSharedPaneWidth(narrow, 900)).toBeGreaterThan(narrow * MAX_PANE_SHARE);
  });

  test("degenerate container: the pane keeps its floor and the list scrolls", () => {
    expect(clampSharedPaneWidth(200, 190)).toBe(SIDE_PANE_MIN_WIDTH);
  });
});

describe("dragPaneFrac", () => {
  test("turns the cursor's distance from the right edge into a fraction", () => {
    // A cursor comfortably inside both the floor and the share cap, so the
    // number that comes back is the drag's own and not a clamp's.
    expect(dragPaneFrac(1000, 500)).toBe(0.5);
  });

  test("the fraction carries the clamp, not the raw pixels", () => {
    // Dragged past the right edge: clamped to the pane's floor, expressed as a
    // share of the container it is in.
    expect(dragPaneFrac(1000, 20)).toBeCloseTo(SIDE_PANE_MIN_WIDTH / 1000, 10);
    // Dragged over the list: the pixel floor alone would clamp to
    // container - 60 (94%), but MAX_PANE_SHARE catches it first at 70% — a
    // LOCAL drag is bounded exactly like an imported width now (second
    // review pass: rendering and the stored commit must agree, so the same
    // clamp has to answer both).
    expect(dragPaneFrac(1000, 990)).toBe(MAX_PANE_SHARE);
  });

  test("a container too narrow for both floors expresses no split at all", () => {
    // Under both floors together the clamp returns PANE_MIN_W whatever the
    // cursor does, so any fraction it yielded would describe the CONTAINER, not
    // a choice — at the floor's own width exactly 1.0, "the pane takes
    // everything", which no wider window can honour. One drag in a narrow pane
    // used to persist that and leave the list a 60px sliver forever after.
    expect(dragPaneFrac(SIDE_PANE_MIN_WIDTH, 170)).toBeNull();
    expect(dragPaneFrac(SIDE_PANE_MIN_WIDTH, 300)).toBeNull();
    expect(dragPaneFrac(SIDE_PANE_MIN_WIDTH + LIST_MIN_W - 1, 100)).toBeNull();
  });

  test("both floors together is the narrowest container that still means something", () => {
    // They fit exactly, so the split is decided even though it has only one
    // possible value.
    const narrow = SIDE_PANE_MIN_WIDTH + LIST_MIN_W;
    expect(dragPaneFrac(narrow, 500)).toBeCloseTo(SIDE_PANE_MIN_WIDTH / narrow, 10);
    expect(dragPaneFrac(narrow, 0)).toBeCloseTo(SIDE_PANE_MIN_WIDTH / narrow, 10);
  });

  test("the fraction a real drag produces can never reach 1, or exceed MAX_PANE_SHARE once the container is wide enough", () => {
    // Below `PANE_MIN_W / MAX_PANE_SHARE` the pane's own floor asks for a
    // bigger share than the cap allows, and floor-last means the floor wins —
    // so a container just inside that band is governed by the pixel floor
    // alone. (At a 400px floor the band runs to ~571px, where it ran to ~314
    // at 220 — the edges move with the floor, which is why the test derives
    // them rather than naming them.)
    const inBand = Math.floor(SIDE_PANE_MIN_WIDTH / MAX_PANE_SHARE) - 20;
    expect(dragPaneFrac(inBand, 600)).toBeCloseTo(SIDE_PANE_MIN_WIDTH / inBand, 10);
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
// D460: the pane's stored width is the SAME pixel number the file sidebar
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
    // 100px is below both this pane's floor and the file sidebar's 380px one,
    // so no ordinary drag on either surface produces it — the clamp still has
    // to hold for whatever arrives.
    expect(paneFracFromSharedWidth(100, 1200)).toBeCloseTo(SIDE_PANE_MIN_WIDTH / 1200, 10);
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
  // (D460's own follow-up): a width dragged wide on the FILE SIDEBAR of a
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

  test("a container under both floors together ignores the shared width entirely", () => {
    // Below PANE_MIN_W + LIST_MIN_W, clampSharedPaneWidth returns PANE_MIN_W
    // regardless of input — more pixels than the container has — and dividing
    // it out would answer a fraction over 1 (`flexBasis: "110%"`), which
    // dragPaneFrac itself refuses to produce (it answers null there). This
    // module has no null to hand back, so it falls back to the plain companion
    // share instead, unconditionally, before the shared width is even read.
    const under = SIDE_PANE_MIN_WIDTH + LIST_MIN_W - 1;
    expect(paneFracFromSharedWidth(900, 200)).toBe(companionFrac(200));
    expect(paneFracFromSharedWidth(900, under)).toBe(companionFrac(under));
    expect(paneFracFromSharedWidth(900, 200)).toBeLessThanOrEqual(1);
  });

  test("both floors together is still the narrowest the shared width can reach — and the FLOOR wins there, not the share cap", () => {
    // They fit exactly, so unlike the degenerate case above the shared width IS
    // honoured and clamped. But the floor's share of that container is ABOVE
    // MAX_PANE_SHARE, so a share cap alone would ask for fewer pixels than the
    // floor and CSS's own `min-width` would override the computed flex-basis.
    // The floor is applied LAST specifically to avoid that: it wins outright in
    // this narrow band (up to `PANE_MIN_W / MAX_PANE_SHARE`), exactly as it did
    // before the cap existed.
    const narrow = SIDE_PANE_MIN_WIDTH + LIST_MIN_W;
    expect(narrow * MAX_PANE_SHARE).toBeLessThan(SIDE_PANE_MIN_WIDTH);
    expect(paneFracFromSharedWidth(900, narrow)).toBeCloseTo(SIDE_PANE_MIN_WIDTH / narrow, 10);
  });

  test("MAX_PANE_SHARE only becomes the ceiling once the container is wide enough that it exceeds the floor", () => {
    // Just past `PANE_MIN_W / MAX_PANE_SHARE`, the share cap asks for more
    // pixels than the floor does, and the cap takes over.
    expect(paneFracFromSharedWidth(900, Math.ceil(SIDE_PANE_MIN_WIDTH / MAX_PANE_SHARE) + 6)).toBe(
      MAX_PANE_SHARE,
    );
  });
});

// ---------------------------------------------------------------- drag close
// The listing pane's version of the sidebars' drag-to-close (#680): between the
// pane's floor and `closeOverdrag(floor)` short of it the clamp renders the
// resistance band, and only a pull clean through reads as "shut it". The band
// scales with the floor — it is the same `closeOverdrag` every panel uses — so
// these are derived rather than named.
describe("paneDragCloses", () => {
  const SHUT = SIDE_PANE_MIN_WIDTH - closeOverdrag(SIDE_PANE_MIN_WIDTH);

  test("a drag through the resistance band closes", () => {
    expect(paneDragCloses(1000, SHUT - 1)).toBe(true);
    expect(paneDragCloses(1000, 0)).toBe(true);
    expect(paneDragCloses(1000, -50)).toBe(true);
  });

  test("holding inside the band, or above the floor, does not", () => {
    expect(paneDragCloses(1000, SHUT)).toBe(false); // the band's own edge sticks
    expect(paneDragCloses(1000, SIDE_PANE_MIN_WIDTH - 1)).toBe(false);
    expect(paneDragCloses(1000, 900)).toBe(false);
  });

  test("the band is the app's own, not a number of this pane's", () => {
    // `closeOverdrag` is what the sidebars pull through too; the pane read a
    // hand-rolled `PANE_MIN_W / 2` until 2026-09-14, which was the same
    // arithmetic in a file that did not notice when the floor moved.
    expect(SHUT).toBe(SIDE_PANE_MIN_WIDTH - Math.floor(SIDE_PANE_MIN_WIDTH / 2));
  });

  test("a container too narrow to express a split never closes by drag", () => {
    // dragPaneFrac is null there — the pane holds its floor whatever the
    // cursor does, so there is no band whose crossing could mean anything.
    expect(paneDragCloses(279, 0)).toBe(false);
    expect(paneDragCloses(0, 0)).toBe(false);
    expect(paneDragCloses(Number.NaN, 0)).toBe(false);
  });
});
