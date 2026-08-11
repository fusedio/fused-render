// What the crumb bar gives up while a folder search is running, guarded at the
// SOURCE. There is no DOM in this suite, so what is testable is the mechanism,
// not the geometry (real layout still needs a browser — see the report).
//
// The bar is a flex row and its `searching` state is already expressed purely in
// CSS (`#breadcrumb:has(.listing-search.searching)`, set by Listing.tsx from a
// non-empty query), so everything here hangs off that one selector. Two things
// are worth pinning down:
//
//   1. HOW the star is removed. `visibility: hidden` or `opacity: 0` keeps the
//      element's box — and the star's box is 24px plus a deliberate -11px
//      negative margin (explorer.css) — so the input would stop 13px short of
//      where the star was, i.e. a hole. In a flex row `display: none` takes the
//      item out of the layout entirely, gap included, which is what "expand
//      completely" needs. This is NOT the column-shedding case
//      (column-shedding.test.ts): there the shed element had to STAY rendered
//      because a table column under `table-layout: fixed` survives its header
//      being hidden and WebKit then split the remainder between two width-less
//      columns. A flex item has no such ghost.
//   2. That the star is HIDDEN, not unmounted. `useUpdateButton` in
//      Breadcrumb.tsx has deliberate side-effect semantics — an armed bookmark
//      disarms permanently on certain changes — and BookmarkStar deletes the
//      matching bookmark on click. Unmounting the subtree on the first keystroke
//      and remounting it on Esc is a state change nobody asked for; CSS is not.
//
// Reversibility comes free from the same fact: the query going empty drops the
// `.searching` class, so every rule here stops applying. Nothing is set from JS,
// so there is no stale state to restore.
import { expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const CSS = readFileSync(join(import.meta.dir, "../../../styles/explorer.css"), "utf8")
  // Comments quote these selectors verbatim; a rule scan that kept them would
  // "find" rules that do not exist.
  .replace(/\/\*[\s\S]*?\*\//g, "");
const BREADCRUMB = readFileSync(join(import.meta.dir, "../Breadcrumb.tsx"), "utf8");

const SEARCHING = ".listing-search.searching";

/** Every declaration block whose selector is the searching state. */
function searchingRules(): { selector: string; decls: string }[] {
  const out: { selector: string; decls: string }[] = [];
  const re = /([^{}]+)\{([^{}]*)\}/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(CSS)) !== null) {
    const selector = m[1].trim();
    if (selector.includes(SEARCHING)) out.push({ selector, decls: m[2] });
  }
  return out;
}

const rules = searchingRules();

function ruleFor(fragment: string): { selector: string; decls: string } | undefined {
  return rules.find((r) => r.selector.includes(fragment));
}

test("the searching state is expressed in CSS at all", () => {
  // If this fails the rest of the file is asserting nothing.
  expect(rules.length).toBeGreaterThanOrEqual(4);
});

test("the bar stands down everything to the left of the search box", () => {
  // The crumbs and the path `···` were already given up; the ★ is what was
  // still holding the box off the left edge.
  for (const target of [".crumbs", "> .bar-overflow", ".bookmark-star-btn"]) {
    const rule = ruleFor(target);
    expect(rule, `no searching rule for ${target}`).toBeDefined();
    expect(rule!.decls).toMatch(/display:\s*none/);
  }
});

test("the star is removed from the layout, not merely made invisible", () => {
  // Its box is 24px wide and carries a -11px right margin to pull the first
  // crumb close (explorer.css). visibility/opacity keep both, so the input
  // would start 13px in from where the star was.
  const decls = ruleFor(".bookmark-star-btn")!.decls;
  expect(decls).not.toMatch(/visibility:\s*hidden/);
  expect(decls).not.toMatch(/opacity:\s*0/);
});

test("hiding the star is scoped to the main crumb bar", () => {
  // Panel mode renders one star per pane out of the same component
  // (Breadcrumb.tsx `BookmarkStar`), and those bars have no search row — an
  // unscoped `.bookmark-star-btn { display: none }` would be a live grenade the
  // day one gains one.
  const selector = ruleFor(".bookmark-star-btn")!.selector;
  expect(selector).toMatch(/#breadcrumb/);
});

test("both the row and the box are told to grow, or the field cannot span the bar", () => {
  // Idle they are `0 1 auto` / a 150px width, so freeing the crumb width is only
  // half of it: without grow on BOTH the input sits at its resting size with the
  // freed space dead beside it.
  for (const target of [".listing-search", ".listing-search-box"]) {
    const rule = rules.find(
      (r) => r.selector.endsWith(target) && /flex:\s*1\s+1/.test(r.decls),
    );
    expect(rule, `no grow rule for ${target}`).toBeDefined();
  }
});

test("the star is rendered unconditionally, so search cannot disarm a bookmark", () => {
  // An armed bookmark disarms permanently on certain changes (useUpdateButton),
  // and the star owns the delete-on-click toggle. Mounting it on the state of a
  // search query would tie both to a keystroke.
  const uses = BREADCRUMB.match(/<BookmarkStar\b[^/]*\/>/g) || [];
  expect(uses.length).toBeGreaterThanOrEqual(2); // the bar and the static bar
  for (const use of uses) {
    // No `{searching && …}` / ternary guard wrapped around the element.
    const at = BREADCRUMB.indexOf(use);
    const before = BREADCRUMB.slice(Math.max(0, at - 40), at);
    expect(before).not.toMatch(/[&?]\s*$|\{\s*!?\w+\s*&&\s*$/);
  }
});
