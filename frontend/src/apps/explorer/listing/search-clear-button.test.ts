// A real clear-search control, not the suppressed native WebKit cancel
// button (that collision with the count chip is why it was hidden in the
// first place). Same CSS-parsing pattern as search-bar-expand.test.ts: read
// explorer.css and SearchField.tsx as text, no DOM in this suite. The box's
// own markup lives in SearchField.tsx (both the folder host, Listing.tsx, and
// the file host, FileSearchField.tsx, render it) rather than in either host.
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

test("the clear button sits between the count chip and the star, and the star stays last", () => {
  const countAt = LISTING.indexOf('className="listing-search-count"');
  const clearAt = LISTING.indexOf('className="listing-search-clear"');
  const starAt = LISTING.indexOf("<BookmarkStar");
  expect(countAt).toBeGreaterThan(-1);
  expect(clearAt).toBeGreaterThan(-1);
  expect(starAt).toBeGreaterThan(-1);
  expect(clearAt).toBeGreaterThan(countAt);
  expect(starAt).toBeGreaterThan(clearAt);
});

test("shown only while the query is non-empty, nothing else claiming its else-branch", () => {
  const at = LISTING.indexOf('className="listing-search-clear"');
  const before = LISTING.slice(Math.max(0, at - 200), at);
  expect(before).toMatch(/hasClear\s*&&\s*\(/);
  const hasClearDef = LISTING.indexOf("const hasClear = query !== \"\";");
  expect(hasClearDef).toBeGreaterThan(-1);
});

test("carries the required aria-label", () => {
  const at = LISTING.indexOf('className="listing-search-clear"');
  const nearby = LISTING.slice(at, at + 300);
  expect(nearby).toMatch(/aria-label="Clear search"/);
});

test("clicking it clears via mousedown+preventDefault, never a click handler that would fire after blur", () => {
  const at = LISTING.indexOf('className="listing-search-clear"');
  const nearby = LISTING.slice(at, at + 500);
  expect(nearby).toMatch(/onMouseDown=/);
  expect(nearby).not.toMatch(/onClick=/);
  expect(nearby).toMatch(/preventDefault\(\)/);
  expect(nearby).toMatch(/clearSearchQuery\(\)/);
});

test("the shared teardown clears the query and unpins, but never blurs — only Escape's own handler blurs", () => {
  const at = LISTING.indexOf("const clearSearchQuery = () => {");
  expect(at).toBeGreaterThan(-1);
  const body = LISTING.slice(at, LISTING.indexOf("};", at));
  expect(body).toMatch(/setQuery\(""\)/);
  expect(body).toMatch(/setPinnedOpen\(false\)/);
  expect(body).not.toMatch(/blur\(\)/);
});

test("styled as a quiet pill matching the star's own token pair", () => {
  const decls = rulesFor(".listing-search-clear");
  expect(decls.length).toBe(1);
  const decl = decls[0];
  expect(decl).toMatch(/background:\s*var\(--ctl-quiet-bg\)/);
  const hoverDecls = rulesFor(".listing-search-clear:hover");
  expect(hoverDecls.length).toBe(1);
  expect(hoverDecls[0]).toMatch(/background:\s*var\(--ctl-quiet-bg-hover\)/);
});

test("sits inboard of the star inside the claimed-folder crumb bar", () => {
  const decls = rulesFor(".crumb-search-slot .listing-search-box .listing-search-clear");
  expect(decls.length).toBe(1);
  expect(decls[0]).toMatch(/right:\s*38px/);
});

// The trailing decorative magnifier is gone: the mode chip at the field's
// leading edge (search-mode-chip.test.ts) now carries that meaning, and two
// magnifiers in one field would say the same thing twice. Nothing with
// class "listing-search-glyph" is emitted or styled any more — the clear
// button's own else-branch is empty, not a second glyph.
test("no trailing magnifier survives, in markup or in CSS", () => {
  expect(LISTING).not.toMatch(/listing-search-glyph/);
  expect(rulesFor(".listing-search-glyph").length).toBe(0);
  expect(rulesFor(".crumb-search-slot .listing-search-box .listing-search-glyph").length).toBe(0);
});

test("the input reserves no left gutter keyed to the magnifier's old name", () => {
  const decls = rulesFor(".listing-search .listing-search-input");
  expect(decls.length).toBe(1);
  expect(decls[0]).not.toMatch(/padding-left/);
});

// The keyboard hint takes over the vacated trailing slot, but only once the
// field is both unfocused and empty — `hasClear` alone would leave the hint
// painting over a blurred-with-a-query field's clear button.
test("the keyboard hint occupies the trailing slot only when unfocused and empty", () => {
  const at = LISTING.indexOf('className="listing-search-shortcut-hint"');
  expect(at).toBeGreaterThan(-1);
  const before = LISTING.slice(Math.max(0, at - 200), at);
  expect(before).toMatch(/!pinnedOpen\s*&&\s*!hasClear\s*&&\s*\(/);
  // Decoration, not a control: no click handler of its own.
  const nearby = LISTING.slice(at, at + 200);
  expect(nearby).not.toMatch(/onMouseDown=/);
  expect(nearby).not.toMatch(/onClick=/);
});

test("the keyboard hint sits at the box's trailing edge, sharing the clear button's own position", () => {
  // Two exact matches for this selector: the base rule and the narrow-width
  // `display: none` override nested under the container query — the base
  // rule is the one written first in the file.
  const decls = rulesFor(".listing-search-shortcut-hint");
  expect(decls.length).toBe(2);
  const base = decls[0];
  expect(base).toMatch(/position:\s*absolute/);
  expect(base).toMatch(/right:\s*8px/);
  expect(base).toMatch(/pointer-events:\s*none/);
  // No left-gutter rule of its own.
  expect(base).not.toMatch(/left:/);
});

test("the keyboard hint moves in to clear the star inside the claimed-folder crumb bar, same offset as the clear button", () => {
  const decls = rulesFor(".crumb-search-slot .listing-search-box .listing-search-shortcut-hint");
  expect(decls.length).toBe(1);
  expect(decls[0]).toMatch(/right:\s*38px/);
});
