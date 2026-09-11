// FINDING 2 (Cursor Bugbot, PR #1092, verified against source): the shortcut
// button (SearchField.tsx) collapses from the wide "Search"+`⌘L` label to
// the bare `<SearchGlyph />` when `boxWide` goes false — driven by
// `HINT_WIDE_PX` (340px), the SAME measurement the placeholder's own long/
// short switch uses. The button used to also go fully `display: none` at a
// container query keyed to a DIFFERENT number, 360px — ABOVE 340px. Since
// both rules watch the same element (`.listing-search-box`), that ordering
// was backwards: at every width the CSS query's 360px covered, the JS
// threshold's own 340px had already decided which BRANCH to render, but the
// CSS hid the result outright before it could ever be seen. The button
// either showed the full label (>360px, where `boxWide` is also already
// true) or vanished (<=360px, which swallows the whole 0-340px range the
// glyph branch renders in) — the collapsed glyph state was unreachable.
//
// The fix keeps `boxWide`'s own 340px as the wide/glyph switch and moves the
// container query's hide threshold below it, so the true ordering — wide
// label, then glyph, then hidden — has a real, non-empty width span for
// each state. Same text-parsing approach as search-hint-width.test.ts's
// sibling suites: no container-query engine in this suite, just the two
// numbers pinned against each other so they can never cross again.
import { expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const SEARCH_FIELD = readFileSync(join(import.meta.dir, "../SearchField.tsx"), "utf8");
const CSS = readFileSync(join(import.meta.dir, "../../../styles/explorer.css"), "utf8").replace(
  /\/\*[\s\S]*?\*\//g,
  "",
);

function hintWidePx(): number {
  const m = SEARCH_FIELD.match(/HINT_WIDE_PX = (\d+)/);
  expect(m, "HINT_WIDE_PX not found in SearchField.tsx").not.toBeNull();
  return Number(m![1]);
}

/** The container-query breakpoint that hides `.listing-search-shortcut-hint`
 * outright (as opposed to the label/glyph switch, which is plain JS state,
 * not CSS). */
function shortcutHideBreakpointPx(): number {
  const re = /@container \(max-width: (\d+)px\) \{\s*\.listing-search-shortcut-hint \{\s*display: none;/;
  const m = CSS.match(re);
  expect(m, "no @container rule hiding .listing-search-shortcut-hint").not.toBeNull();
  return Number(m![1]);
}

test("the button's wide-label threshold (HINT_WIDE_PX) is exactly 340px", () => {
  // Pinned by name/value together — a drift here is exactly the kind of
  // silent divergence this file exists to catch.
  expect(hintWidePx()).toBe(340);
});

test("there is exactly one @container rule that fully hides the shortcut button", () => {
  const occurrences = [
    ...CSS.matchAll(/@container \(max-width: \d+px\) \{\s*\.listing-search-shortcut-hint \{\s*display: none;/g),
  ];
  expect(occurrences.length).toBe(1);
});

test("the hide breakpoint sits BELOW the wide-label threshold, leaving the collapsed glyph a real width range", () => {
  const wide = hintWidePx();
  const hide = shortcutHideBreakpointPx();
  expect(hide).toBeLessThan(wide);
  // Not a knife-edge: the glyph-only state (hide < width < wide) needs
  // enough of a span to actually be reachable at typical panel widths, not
  // just a single pixel a real browser would never land on.
  expect(wide - hide).toBeGreaterThanOrEqual(50);
});

test("the hide breakpoint itself is comfortably above the box's own squeeze floor (124px, crumb-slot)", () => {
  // Below this the box can't get any narrower at all (min-width, above) —
  // a hide threshold below or at the floor would mean the button is either
  // always hidden or never, defeating the point of a threshold.
  const floorMatch = CSS.match(/\.crumb-search-slot \.listing-search-box \{[^}]*min-width: (\d+)px;/);
  expect(floorMatch, "crumb-slot box min-width not found").not.toBeNull();
  const floor = Number(floorMatch![1]);
  const hide = shortcutHideBreakpointPx();
  expect(hide).toBeGreaterThan(floor);
});
