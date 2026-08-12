// The tight-bar fold's two convergence invariants, guarded at the SOURCE.
//
// The fold (Listing.tsx's measurement) is a setState driven by a DOM
// measurement, in a layout effect with no dependency array — i.e. a feedback
// loop, where the thing being measured is changed by the answer. Such a loop is
// only safe while two properties hold, and violating EITHER one blanked the
// whole explorer with React error #185 ("maximum update depth exceeded") the
// moment a second row was selected: the fold flipped on every commit and React
// gave up, taking the view down with it.
//
// There is no DOM in this suite (see search-bar-expand.test.ts, same idiom), so
// what is testable is the mechanism rather than the geometry — but the mechanism
// is exactly what regressed, and both invariants are visible in the source.
//
//   1. THE HYSTERESIS BAND COVERS THE BOX. Unfolding hands the strip back a box
//      of `width`, so the unfold threshold has to BE that width. It is not one
//      number — a box with a chip pinned in it (`.has-pin`, in practice the
//      multi-selection readout) is 260px against an idle 150px — so the
//      threshold is read from `--resting-width` at measure time instead of being
//      hardcoded. This test pins the property to the `width` beside it: the two
//      drifting apart is the bug, and a hardcoded 150 against a 260px box is
//      that bug at its widest.
//
//   2. THE UPDATER IS PURE. React evaluates an updater function on its own
//      schedule — eagerly when the setter is called, to test for a bail-out, and
//      again while rendering — so an updater that MEASURES THE DOM answers a
//      different question each time it runs. The fold therefore computes its
//      next value from the DOM first and passes `setTightBar` a plain boolean,
//      and it reads which state that DOM is in from the DOM too (`.iconized`),
//      so the widths and the state they describe always come from one layout.
import { expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const CSS = readFileSync(
  join(import.meta.dir, "../../../styles/explorer.css"),
  "utf8",
  // Comments quote these selectors and numbers verbatim; a scan that kept them
  // would "find" declarations that do not exist.
).replace(/\/\*[\s\S]*?\*\//g, "");

const LISTING = readFileSync(join(import.meta.dir, "../Listing.tsx"), "utf8");

/** Every declaration block whose selector contains `fragment`. */
function rulesFor(fragment: string): { selector: string; decls: string }[] {
  const out: { selector: string; decls: string }[] = [];
  const re = /([^{}]+)\{([^{}]*)\}/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(CSS)) !== null) {
    const selector = m[1].trim();
    if (selector.includes(fragment)) out.push({ selector, decls: m[2] });
  }
  return out;
}

const decl = (decls: string, prop: string): string | null => {
  const m = new RegExp(`(?:^|;)\\s*${prop}\\s*:\\s*([^;]+)`).exec(decls);
  return m ? m[1].trim() : null;
};

// The box's own rules in slot mode — the only place it is given a width.
const boxRules = rulesFor(".crumb-search-slot").filter((r) =>
  r.selector.includes(".listing-search-box"),
);

test("the search box is sized in CSS at all", () => {
  // If this fails the rest of the file is asserting nothing.
  expect(boxRules.length).toBeGreaterThanOrEqual(2);
  expect(boxRules.some((r) => decl(r.decls, "width") !== null)).toBe(true);
});

test("every width the box can rest at declares the same --resting-width", () => {
  // The threshold and the width are the SAME fact, and this is what stops them
  // being edited apart. A rule that sets one without the other is the
  // regression: `.has-pin` grew the box to 260px while the fold still asked for
  // 150px of free space, so 150px of slack was enough to unfold into and
  // nowhere near enough to hold the box — the crumbs re-ellipsized, the bar
  // folded, the freed slack cleared 150 again, forever.
  const sized = boxRules.filter((r) => decl(r.decls, "width") !== null);
  expect(sized.length).toBeGreaterThanOrEqual(2); // idle + .has-pin

  for (const rule of sized) {
    const width = decl(rule.decls, "width");
    const resting = decl(rule.decls, "--resting-width");
    // The fold state's own rule sets width:0 and is exempt: a folded box has to
    // keep reporting the width it will UNFOLD to, which is the whole reason the
    // threshold rides a custom property instead of `width` (custom properties
    // are not overridden by that rule).
    if (rule.selector.includes(".iconized")) {
      expect(width).toBe("0");
      expect(resting, `${rule.selector} must not restate --resting-width`).toBe(null);
      continue;
    }
    expect(resting, `${rule.selector} sets width but no --resting-width`).toBe(width);
  }
});

test("the folded box still reports the width it would unfold to", () => {
  // i.e. the two resting widths are actually different numbers, so the previous
  // test is guarding a real distinction and not two copies of one value.
  const values = new Set(
    boxRules
      .map((r) => decl(r.decls, "--resting-width"))
      .filter((v): v is string => v !== null),
  );
  expect(values.size).toBeGreaterThanOrEqual(2);
});

test("the fold's threshold is read from CSS, never hardcoded", () => {
  expect(LISTING).toContain("--resting-width");
  // The old constant, in the comparison that used it. The fallback for the
  // frame before the stylesheet lands is allowed to name 150 — what must not
  // come back is a bare number on the free-space test.
  expect(LISTING).not.toMatch(/freeInBar\(\)\s*<\s*\d/);
  expect(LISTING).toMatch(/freeInBar\(\)\s*<\s*restingWidth\(\)/);
});

test("the fold never measures the DOM inside a state updater", () => {
  // `setTightBar(fn)` hands React a function it may run more than once per
  // commit, at moments when the DOM does not match the state passed in. The
  // measurement must therefore be finished BEFORE the setter is called, and the
  // setter handed a plain value.
  expect(LISTING).toMatch(/setTightBar\(/);
  expect(LISTING).not.toMatch(/setTightBar\(\s*\(/); // no updater callback
  expect(LISTING).not.toMatch(/setTightBar\(\s*folded\s*=>/);
});

test("the fold reads its current state from the layout it measured", () => {
  // Not from React state: the widths come from the DOM, so the answer to "is
  // the box folded right now" has to come from the same DOM or the decision is
  // made on a mismatched pair.
  expect(LISTING).toMatch(/classList\.contains\("iconized"\)/);
  // And that class is the one the row actually renders, so the two agree.
  expect(LISTING).toMatch(/" iconized"/);
});
