// What the crumb bar gives up while a folder search is running, guarded at the
// SOURCE. There is no DOM in this suite, so what is testable is the mechanism,
// not the geometry (real layout still needs a browser — see the report).
//
// The bar is a flex row and its `searching` state is already expressed purely in
// CSS (`#breadcrumb:has(.listing-search.searching)`, set by Listing.tsx from a
// non-empty query), so everything here hangs off that one selector. What is
// worth pinning down:
//
//   HOW the history arrows are removed. `visibility: hidden` or `opacity: 0`
//   keeps the element's box — the arrows are 28px each — so the field would
//   stop short of where they were, i.e. a hole. In a flex row `display: none`
//   takes the item out of the layout entirely, gap included, which is what
//   "expand completely" needs. This is NOT the column-shedding case
//   (column-shedding.test.ts): there the shed element had to STAY rendered
//   because a table column under `table-layout: fixed` survives its header
//   being hidden and WebKit then split the remainder between two width-less
//   columns. A flex item has no such ghost.
//
// The star used to stand down here too, alongside the arrows — it sat outside
// the field, on the bar's own left/outer zone. It now lives INSIDE the field
// (Listing.tsx renders it as the box's own trailing affordance, past the
// count/spinner pin — see explorer.css), so it grows and shrinks with the box
// rather than getting hidden by the bar around it; there is nothing left for a
// searching-scoped rule to say about it.
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
  expect(rules.length).toBeGreaterThanOrEqual(3);
});

test("the bar stands down the history arrows for the search box", () => {
  // `.crumb-nav` is what is still holding the box off the left edge — the star
  // no longer is (see below). There is no `.crumbs` entry here any more: a
  // claimed folder never renders a `.crumbs` element at all (Breadcrumb.tsx:
  // `claimed ? null : <div className="crumbs">`), so there is nothing under
  // that class for a searching rule to stand down. The path `···` needs no
  // rule either, for the same reason it never did — it is the bar's
  // right-click menu now (Breadcrumb's onBarContextMenu), not markup in the
  // bar.
  const rule = ruleFor(".crumb-nav");
  expect(rule, "no searching rule for .crumb-nav").toBeDefined();
  expect(rule!.decls).toMatch(/display:\s*none/);
});

test("the arrows are removed from the layout, not merely made invisible", () => {
  // Each is a 28px square; visibility/opacity would keep both boxes and leave
  // the field starting in from a 56px hole where they were.
  const decls = ruleFor(".crumb-nav")!.decls;
  expect(decls).not.toMatch(/visibility:\s*hidden/);
  expect(decls).not.toMatch(/opacity:\s*0/);
});

test("the path zone runs arrows, path, star — the order the unclaimed bar still keeps", () => {
  // Only the UNCLAIMED bar (a file view, or StaticBreadcrumb's own label row)
  // still has this trio: over a claimed folder there is no `.crumbs` at all
  // (the star lives inside the field there instead — see the header). For the
  // trio that remains, the ordering is what the star's negative margin is
  // aimed at (`margin-left` tightens last-crumb/★) and what the auto margin
  // that eats the bar's slack rides on as the zone's true tail
  // (`.crumbs:has(.path-crumb) ~ .bookmark-star-btn`, explorer.css). Flip any
  // of the three and the bar silently regrows a gap no rule here would catch.
  const nav = BREADCRUMB.indexOf("<CrumbNav />");
  const crumbs = BREADCRUMB.indexOf('<div className="crumbs"');
  const star = BREADCRUMB.indexOf('<BookmarkStar id="bookmark-btn"');
  expect(nav).toBeGreaterThan(-1);
  expect(crumbs).toBeGreaterThan(nav);
  expect(star).toBeGreaterThan(crumbs);
});

test("the star no longer stands down with the bar — it lives inside the field now", () => {
  // This used to be "hiding the star is scoped to #breadcrumb" (Panel mode
  // renders one star per pane out of the same component, and those bars have
  // no search row, so an unscoped rule would have been a live grenade the day
  // one gains one). The star does not hide here at all any more: it moved off
  // the bar's outer zone and into the field itself (Listing.tsx, past the
  // count/spinner pin — explorer.css), where it grows and shrinks with the
  // box instead of standing down around it. There is nothing left to scope.
  expect(ruleFor(".bookmark-star-btn")).toBeUndefined();
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
