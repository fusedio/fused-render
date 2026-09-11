// THE DEFECT (running-screen review, reported with a screenshot): in the
// merged crumb-bar omnibox, at rest (field NOT focused, crumbs showing), the
// bookmark star sat directly on top of the last crumb's text — the path
// `~ / Downloads / Archive` rendered with the star's tinted hover pill
// covering the last two letters of "Archive".
//
// Root cause: `.listing-search-crumbs` (explorer.css) reserves only
// `right: 10px` — enough to clear the box's own border, not the star, which
// sits at `right: 8px` in a 24px hit box
// (`#breadcrumb .crumb-search-slot .listing-search-box .bookmark-star-btn`).
// A short path never shows this because the crumbs are left-aligned, but
// PathCrumbs.tsx deliberately pins the strip's scrollLeft to its own END (so
// a narrow field keeps the CURRENT folder readable), which drives a long
// path's tail straight under the star.
//
// The fix reuses `--pin-right` (already 40px on
// `.crumb-search-slot .listing-search-box`, set for exactly this same star
// clearance so the count/spinner pin doesn't collide with it either) rather
// than inventing a second number for the same distance.
//
// Same CSS-parsing pattern as search-mode-chip.test.ts and
// search-count-pin-degrade.test.ts: read explorer.css as text (comments
// stripped) and assert on the selectors/declarations present — no container-
// query or layout engine in this suite's DOM to render against.
import { expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const CSS = readFileSync(join(import.meta.dir, "../../../styles/explorer.css"), "utf8").replace(
  /\/\*[\s\S]*?\*\//g,
  "",
);

function rulesFor(selectorExact: string): string[] {
  const out: string[] = [];
  const re = /([^{}]+)\{([^{}]*)\}/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(CSS)) !== null) {
    if (m[1].trim() === selectorExact) out.push(m[2]);
  }
  return out;
}

test("the crumb-slot host's crumbs strip reserves the star's own clearance (--pin-right), not a second hardcoded number", () => {
  const rules = rulesFor("#breadcrumb .crumb-search-slot .listing-search-box .listing-search-crumbs");
  expect(rules.length).toBe(1);
  expect(rules[0]).toMatch(/right:\s*var\(--pin-right\)/);
  // Not a literal pixel value duplicated alongside the property that already
  // carries this exact distance.
  expect(rules[0]).not.toMatch(/right:\s*\d/);
});

test("--pin-right is set on the crumb-slot's own box (not read from nowhere) at the value tuned to clear the star", () => {
  const boxRules = rulesFor(".crumb-search-slot .listing-search-box");
  expect(boxRules.length).toBeGreaterThan(0);
  expect(boxRules.some((r) => /--pin-right:\s*40px/.test(r))).toBe(true);
});

test("the base, unscoped crumbs rule is untouched — the reservation is additive, scoped only to the host with a star", () => {
  const base = rulesFor(".listing-search-crumbs");
  expect(base.length).toBe(1);
  expect(base[0]).toMatch(/right:\s*10px/);
});

test("the reservation is scoped to the crumb-slot host — the inline/pane copy of the box has no star inside it, so it must not get dead space", () => {
  // The override selector must require the crumb-slot ancestry, not just
  // `.listing-search-box .listing-search-crumbs` generically (which would
  // also match the star-less inline/pane host).
  const overrideExists = CSS.includes(
    "#breadcrumb .crumb-search-slot .listing-search-box .listing-search-crumbs {\n  right: var(--pin-right);\n}",
  );
  expect(overrideExists).toBe(true);
});

test("the star sits inside the SAME crumb-slot scope the crumbs override targets, so the two rules describe the same host", () => {
  const starRules = rulesFor("#breadcrumb .crumb-search-slot .listing-search-box .bookmark-star-btn");
  expect(starRules.length).toBe(1);
  expect(starRules[0]).toMatch(/right:\s*8px/);
});

test("the count/spinner pin cannot coexist with the crumbs strip — both only render on mutually exclusive query states, so no rule is needed to clear the count chip too", () => {
  // The pin (count/spinner) only appears once `query !== ""` (Listing.tsx's
  // `hasPin`, useListingSearch.ts's `runsSearch = searching && !isPathQuery`,
  // and `searching` requires a non-empty query). The crumbs only render
  // while `query === "" && !pinnedOpen` (SearchField.tsx). These are
  // disjoint states by construction, not by coincidence — documented in the
  // comment above the crumbs override rule rather than re-derived here.
  const searchFieldTsx = readFileSync(
    join(import.meta.dir, "../SearchField.tsx"),
    "utf8",
  );
  expect(searchFieldTsx).toMatch(/query === "" && !pinnedOpen/);
});
