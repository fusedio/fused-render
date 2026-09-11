// The field's own mode chip. No DOM in this suite (same text-parsing
// pattern as search-bar-expand.test.ts and search-clear-button.test.ts):
// read SearchField.tsx and explorer.css as text — the box's own markup, and
// both hosts (Listing.tsx over a folder, FileSearchField.tsx over a plain
// file) render this same component rather than each carrying a copy.
//
// SPEC-omnibox-search-affordance.md, scope item 1: the chip lost its word.
// The 12px glyph alone carries the mode now — these tests assert the glyph
// is still there, that a screen reader still gets a spoken label the
// visible word used to carry, and that `--chip-inset` (scope item 2) no
// longer varies by mode now that both states render the same glyph width.
import { expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const CSS = readFileSync(join(import.meta.dir, "../../../styles/explorer.css"), "utf8").replace(
  /\/\*[\s\S]*?\*\//g,
  "",
);
const LISTING = readFileSync(join(import.meta.dir, "../SearchField.tsx"), "utf8");

function rulesFor(selectorExact: string): string[] {
  const out: string[] = [];
  const re = /([^{}]+)\{([^{}]*)\}/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(CSS)) !== null) {
    if (m[1].trim() === selectorExact) out.push(m[2]);
  }
  return out;
}

// The chip's mode is driven off `isPathQuery` (path-shaped-query.ts's
// `isPathShapedQuery`, shape only, never existence) layered under the
// existing `searching` gate — not a second, parallel test for "is this a
// search". `searching` is layered on top only because `isPathQuery` itself
// is true for an empty query too (`listingAddress("")` is null, but an empty
// field renders no chip word choice worth making either way), and the field
// must read "Path" while genuinely idle. `isPathQuery` itself is a prop
// here, computed exactly ONCE — inside `useListingSearch.ts`, off the SAME
// query the hook already owns — and handed down by both hosts (Listing.tsx,
// FileSearchField.tsx) as a plain destructure of their own hook's return, so
// there is no second call site that could compute a different answer for the
// same query.
test("the chip's mode is isPathQuery layered under the existing searching gate, not a second predicate", () => {
  const at = LISTING.indexOf("const chipIsSearch =");
  expect(at).toBeGreaterThan(-1);
  const line = LISTING.slice(at, LISTING.indexOf(";", at) + 1);
  expect(line).toMatch(/searching\s*&&\s*!isPathQuery/);
  const propAt = LISTING.indexOf("isPathQuery: boolean;");
  expect(propAt).toBeGreaterThan(-1);
  expect(propAt).toBeLessThan(at);

  // Both hosts read `isPathQuery` off their own `useListingSearch` call
  // rather than computing it a second way.
  const LISTING_HOST = readFileSync(join(import.meta.dir, "../Listing.tsx"), "utf8");
  const listingDestructure = LISTING_HOST.slice(
    LISTING_HOST.indexOf("const {"),
    LISTING_HOST.indexOf("useListingSearch("),
  );
  expect(listingDestructure).toMatch(/isPathQuery,/);

  const FILE_HOST = readFileSync(join(import.meta.dir, "../FileSearchField.tsx"), "utf8");
  // The file's own header comment mentions `useListingSearch(` too (its own
  // call signature, documented) — the REAL call is the one after `const {`.
  const fileDestructureStart = FILE_HOST.indexOf("const {");
  const fileDestructure = FILE_HOST.slice(
    fileDestructureStart,
    FILE_HOST.indexOf("useListingSearch(", fileDestructureStart),
  );
  expect(fileDestructure).toMatch(/isPathQuery,/);

  // The predicate itself lives in exactly one place: useListingSearch.ts
  // computes it via `isPathShapedQuery`, never re-derived at either host.
  const HOOK = readFileSync(
    join(import.meta.dir, "useListingSearch.ts"),
    "utf8",
  );
  expect(HOOK).toMatch(
    /const isPathQuery = isPathShapedQuery\(query, fsPath, home\);/,
  );
  expect(LISTING_HOST).not.toMatch(/isPathShapedQuery\(/);
  expect(FILE_HOST).not.toMatch(/isPathShapedQuery\(/);
});

// The visible word is gone (scope item 1) — the chip renders only the
// glyph, `aria-hidden`, plus a screen-reader-only label carrying the word
// that used to be visible. Removing the word without the spoken label would
// leave a screen reader announcing nothing at all for the mode.
test("the chip has no visible word, but still carries a spoken mode label", () => {
  const at = LISTING.indexOf('className={"listing-search-mode"');
  expect(at).toBeGreaterThan(-1);
  const block = LISTING.slice(at, LISTING.indexOf("</span>\n", LISTING.indexOf("sr-only", at)) + 8);
  // No bare "Search"/"Path" text node sitting directly in the chip's markup
  // outside of the sr-only span (the sr-only span itself is expected to
  // carry exactly this word, so the check has to exclude it).
  const srOnlyAt = block.indexOf("sr-only");
  expect(srOnlyAt).toBeGreaterThan(-1);
  const beforeSrOnly = block.slice(0, srOnlyAt);
  expect(beforeSrOnly).not.toMatch(/>\s*Search\s*</);
  expect(beforeSrOnly).not.toMatch(/>\s*Path\s*</);
  expect(block).toMatch(/chipIsSearch\s*\?\s*"Search"\s*:\s*"Path"/);
});

// A readout, not a control: pointer-events: none (click lands on the input
// beneath, same trick the crumbs use), and no hover or cursor treatment at
// all, unlike the star's own quiet-pill pair.
test("the chip is a readout: pointer-events none, no hover state, no quiet-pill background", () => {
  const decls = rulesFor(".listing-search-mode");
  expect(decls.length).toBe(1);
  expect(decls[0]).toMatch(/pointer-events:\s*none/);
  expect(decls[0]).not.toMatch(/background/);
  expect(decls[0]).not.toMatch(/cursor/);
  expect(rulesFor(".listing-search-mode:hover").length).toBe(0);
  expect(CSS).not.toMatch(/--ctl-quiet-bg[\s\S]{0,80}\.listing-search-mode/);
});

test("the search state alone takes the accent; the path state stays muted", () => {
  const base = rulesFor(".listing-search-mode");
  expect(base.length).toBe(1);
  expect(base[0]).toMatch(/color:\s*var\(--fg-muted\)/);
  const searchState = rulesFor(".listing-search-mode.search");
  expect(searchState.length).toBe(1);
  expect(searchState[0]).toMatch(/color:\s*var\(--accent\)/);
});

// The crumbs and the input read the SAME custom property for their start
// offset, so a mismatch between the two (a visible text jump on focus) is
// structurally impossible — one value, set in one place, read in two.
test("the crumbs and the input start at the same custom property, not two separately-typed numbers", () => {
  const crumbs = rulesFor(".listing-search-crumbs");
  expect(crumbs.length).toBe(1);
  expect(crumbs[0]).toMatch(/left:\s*var\(--chip-inset\)/);
  const input = rulesFor(".listing-search-input");
  expect(input.length).toBe(1);
  expect(input[0]).toMatch(/padding:\s*6px 10px 6px var\(--chip-inset\)/);
});

// Scope item 2: an icon-only chip is the same width in both modes, so
// `--chip-inset` collapses to one value instead of varying per mode — the
// per-mode override (`.listing-search-box.search`) is now dead and must be
// gone, not just unused.
test("--chip-inset is a single value now that both modes render the same glyph width", () => {
  const base = rulesFor(".listing-search-box");
  expect(base.length).toBe(1);
  expect(base[0]).toMatch(/--chip-inset:\s*\d+px/);
  expect(rulesFor(".listing-search-box.search").length).toBe(0);
  expect(CSS).not.toMatch(/\.listing-search-box\.search\s*\{/);
  // Neither the crumbs nor the input rule sets the variable itself — they
  // only read it.
  const crumbs = rulesFor(".listing-search-crumbs");
  expect(crumbs[0]).not.toMatch(/--chip-inset:/);
  const input = rulesFor(".listing-search-input");
  expect(input[0]).not.toMatch(/--chip-inset:/);
});

// The box no longer needs its own copy of the mode class — nothing left
// reads `--chip-inset` per mode, and no other rule keys off
// `.listing-search-box.search` (asserted above), so the box's classList
// carries no mode modifier of its own any more.
test("the box's own classList no longer carries a chipIsSearch-gated mode class", () => {
  const at = LISTING.indexOf('"listing-search-box" +');
  expect(at).toBeGreaterThan(-1);
  const block = LISTING.slice(at, LISTING.indexOf("(hasPin", at));
  expect(block).not.toMatch(/\(chipIsSearch \? " search" : ""\)/);
});

// Scope item 3, revised (user preference on a running screen): the words
// stay — "Search ⌘L" was never the defect, an unclickable pill wearing no
// chassis was. It is a real button now, on the same `bar-ctl` family the
// neighbouring `⋮` trigger rides, no longer a decoration a press falls
// through, and no longer visible once a query has committed.
test("the hint is a real button now, gone the instant the field takes focus, and while a query is still there to clear", () => {
  const at = LISTING.indexOf('className={"listing-search-shortcut-hint');
  expect(at).toBeGreaterThan(-1);
  const before = LISTING.slice(Math.max(0, at - 200), at);
  expect(before).toMatch(/!pinnedOpen\s*&&\s*!hasClear\s*&&\s*\(/);
  const openTagAt = LISTING.lastIndexOf("<", at);
  expect(LISTING.slice(openTagAt, openTagAt + 7)).toBe("<button");
  const block = LISTING.slice(at, at + 700);
  expect(block).toMatch(/"listing-search-shortcut-hint bar-ctl"/);
});

// The words render at wide width — "Search" as plain text plus the
// platform-conditional shortcut in its key-cap. `boxWide` (the SAME
// measurement the placeholder's own long/short switch already uses)
// collapses this to the bare magnifier at narrow widths, where the
// accessible name (below) becomes the only place the shortcut still
// appears.
test("the words render at wide width: Search plus the platform-conditional shortcut", () => {
  const at = LISTING.indexOf('className={"listing-search-shortcut-hint');
  expect(at).toBeGreaterThan(-1);
  const block = LISTING.slice(at, at + 1100);
  expect(block).toMatch(/boxWide \?/);
  expect(block).toMatch(/\{"Search"\}/);
  expect(block).toMatch(/<kbd>\{isMac \? "⌘L" : "Ctrl L"\}<\/kbd>/);
  // The glyph is the OTHER branch, not a second thing beside the words. It
  // renders through `<SearchGlyph />`, the one magnifier this file draws —
  // shared with the dropdown's search-action row, so the collapsed button and
  // the row that runs the search cannot end up wearing two different icons.
  expect(block.indexOf("{boxWide ?")).toBeLessThan(block.indexOf("<SearchGlyph />"));
  const importAt = LISTING.indexOf('import { isMac } from "@platform/lib/platform";');
  expect(importAt).toBeGreaterThan(-1);
  expect(block).not.toMatch(/navigator/);
});

// The accessible name (aria-label) carries the shortcut in BOTH the wide
// and the collapsed form — in the collapsed form it is the only place the
// shortcut still appears at all, so it can't be conditional on `boxWide`.
test("the accessible name includes Search and the shortcut regardless of width", () => {
  const at = LISTING.indexOf('className={"listing-search-shortcut-hint');
  const block = LISTING.slice(at, at + 700);
  expect(block).toMatch(/aria-label=\{`Search this folder \(\$\{isMac \? "⌘L" : "Ctrl L"\}\)`\}/);
});

// Pressing it must do what ⌘L already does — reusing `requestSearchFocus`
// (listing/search-focus.ts), the exact call Breadcrumb.tsx's own ⌘L
// listener makes, rather than a second path to the same behaviour.
test("the button reuses requestSearchFocus, not a second focus/expand path", () => {
  const importAt = LISTING.indexOf(
    'import { requestSearchFocus, subscribeSearchFocusRequest } from "@apps/explorer/listing/search-focus";',
  );
  expect(importAt).toBeGreaterThan(-1);
  const at = LISTING.indexOf('className={"listing-search-shortcut-hint');
  const block = LISTING.slice(at, at + 700);
  expect(block).toMatch(/requestSearchFocus\(/);
});

// Absent, not clipped, at a narrow width: a CSS container query on the box
// itself, not a measured ref — the branch already shipped a bug of exactly
// that shape (a useLayoutEffect([]) that froze on a null ref).
test("the button disappears at a narrow box width via a container query, not a measured ref", () => {
  const boxDecls = rulesFor(".listing-search-box");
  expect(boxDecls.length).toBe(1);
  expect(boxDecls[0]).toMatch(/container-type:\s*inline-size/);
  // FINDING 2 (code review, 2026-09-10): not 360px — that used to sit ABOVE
  // `boxWide`'s own 340px threshold, hiding the button at every width its
  // collapsed-glyph branch could ever render at (see
  // search-shortcut-collapse.test.ts). The hide breakpoint now sits below
  // 340px instead, so the wide label, the glyph, and hidden each get a real
  // width span.
  const containerRe = /@container \(max-width: (\d+)px\) \{\s*\.listing-search-shortcut-hint \{/;
  const m = CSS.match(containerRe);
  expect(m, "no @container rule hiding .listing-search-shortcut-hint").not.toBeNull();
  expect(Number(m![1])).toBeLessThan(340);
  const containerAt = CSS.indexOf(m![0]);
  const block = CSS.slice(containerAt, containerAt + 200);
  expect(block).toMatch(/\.listing-search-shortcut-hint\s*\{[\s\S]*display:\s*none/);
  // The button itself carries no width measurement of its own — no ref, no
  // ResizeObserver — anywhere near its markup.
  const hintAt = LISTING.indexOf('className={"listing-search-shortcut-hint');
  const nearby = LISTING.slice(Math.max(0, hintAt - 300), hintAt + 300);
  expect(nearby).not.toMatch(/useLayoutEffect|ResizeObserver|useWidthThresholdRef/);
});
