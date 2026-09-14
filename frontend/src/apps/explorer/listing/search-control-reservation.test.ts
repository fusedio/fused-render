// The bug this guards: `computeControlReservation` (search-hint-width.ts)
// used to be `box.right - control.left + gap` with no check on whether
// either element was actually laid out. `.listing-search-shortcut-hint` is
// hidden via `display: none` on a narrow box (explorer.css's `@container
// (max-width: 189px)` rule) — hidden, not unmounted, so the ref stays
// attached and `getBoundingClientRect()` on it returns an all-zero rect
// pinned at the origin. The old arithmetic then published `box.right - 0 +
// gap`, a number on the order of the box's own distance from the viewport's
// left edge — measured live at a 124px box, this came out to 214px, larger
// than the box itself, which collapsed the crumb strip to zero width and
// blanked the breadcrumb path down to a bare folder icon.
//
// jsdom (this test's DOM) always reports zero-size rects for every element,
// hidden or not, so a REAL DOM node can never exercise the "normal case"
// branch here — only a stubbed element with a deliberately non-zero rect
// can, which is why this tests the pure function directly rather than
// mounting the hook.
import { expect, test } from "bun:test";
import { computeControlReservation } from "@apps/explorer/listing/search-hint-width";

/** A stand-in for the parts of `Element` the reservation math reads —
 * `getBoundingClientRect()` and `offsetParent`. Real hidden elements report
 * both a zero-area rect AND a null `offsetParent`; these fakes let a test
 * pick them apart to prove each signal is actually checked. */
function fakeEl(rect: { left?: number; right?: number; width: number; height: number }, laidOut: boolean) {
  return {
    getBoundingClientRect: () => ({ left: 0, right: 0, ...rect }),
    offsetParent: laidOut ? ({} as Element) : null,
  };
}

test("normal case: reservation is box.right - control.left + gap", () => {
  const box = fakeEl({ right: 537, width: 537, height: 32 }, true);
  const control = fakeEl({ left: 402, width: 135, height: 28 }, true);
  expect(computeControlReservation(box, control, 6)).toBeCloseTo(537 - 402 + 6);
});

test("hidden control (display: none, offsetParent null, zero rect) reports null, not a garbage number", () => {
  const box = fakeEl({ right: 124, width: 124, height: 32 }, true);
  // What `display: none` actually produces: offsetParent null AND a
  // zero-area rect pinned at the origin — this is the exact input that used
  // to publish a 214px reservation for a 124px box.
  const hiddenControl = fakeEl({ left: 0, width: 0, height: 0 }, false);
  expect(computeControlReservation(box, hiddenControl, 6)).toBeNull();
});

test("offsetParent null is checked even if a rect somehow reports non-zero", () => {
  // Belt-and-suspenders: offsetParent is the primary "not laid out" signal,
  // independent of the rect. A stale/inconsistent non-zero rect on a
  // display:none element must still be rejected.
  const box = fakeEl({ right: 124, width: 124, height: 32 }, true);
  const control = fakeEl({ left: 10, width: 20, height: 20 }, false);
  expect(computeControlReservation(box, control, 6)).toBeNull();
});

test("zero-area rect is checked even if offsetParent somehow reports non-null", () => {
  // The other half of the belt-and-suspenders: first paint, before layout
  // has run at all, can hand back a non-null offsetParent with a
  // still-zero rect for one frame — this must be discarded too, not
  // published as a reservation of `box.right - 0 + gap`.
  const box = fakeEl({ right: 124, width: 124, height: 32 }, true);
  const control = fakeEl({ left: 0, width: 0, height: 0 }, true);
  expect(computeControlReservation(box, control, 6)).toBeNull();
});

test("hidden box (degenerate box rect) also reports null", () => {
  const hiddenBox = fakeEl({ right: 0, width: 0, height: 0 }, false);
  const control = fakeEl({ left: 402, width: 135, height: 28 }, true);
  expect(computeControlReservation(hiddenBox, control, 6)).toBeNull();
});
