// THE DEFECT (running-screen review, 2026-09-10): a committed search in a
// ~600px-wide box rendered the pin "31 matches · not refreshed" over a
// FIXED input padding-right reservation, clipping a query like
// `~/*/*.zip` down to about four visible characters — even though the
// input's own `value` held the full text. The rule: the query text has
// priority over the pin, always. The pin degrades in three rungs as the
// box narrows (full -> count-only -> nothing), dropping the least
// actionable part (the timing figure / freshness caveat) first, and the
// full text always survives in the pin's own tooltip even once nothing is
// visible.
//
// Same CSS-parsing pattern as search-completion-width.test.ts and
// search-mode-chip.test.ts: read explorer.css and SearchField.tsx as text
// and assert on the selectors/declarations/markup actually present, since
// there is no container-query engine in this suite's DOM to render against
// (bun's jsdom does not evaluate `@container`) — see SPEC's own "Cannot be
// verified headlessly" section for what still needs a human on a running
// screen.
import { expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const CSS = readFileSync(join(import.meta.dir, "../../../styles/explorer.css"), "utf8").replace(
  /\/\*[\s\S]*?\*\//g,
  "",
);
const SEARCH_FIELD = readFileSync(join(import.meta.dir, "../SearchField.tsx"), "utf8");
const LISTING = readFileSync(join(import.meta.dir, "../Listing.tsx"), "utf8");

/** The full text of every `@container (max-width: <px>px) { ... }` block at
 * that exact threshold, joined — captured with a brace-depth walk since
 * these blocks nest a second level of `{}` inside them, which a flat regex
 * can't span. There can be more than one block at the same threshold in
 * this file (deliberate reuse of a number for an unrelated rule, documented
 * where it happens), so this returns ALL of them concatenated rather than
 * just the first match. */
function containerBlock(maxWidthPx: number): string {
  const marker = `@container (max-width: ${maxWidthPx}px)`;
  const blocks: string[] = [];
  let searchFrom = 0;
  for (;;) {
    const at = CSS.indexOf(marker, searchFrom);
    if (at === -1) break;
    const openAt = CSS.indexOf("{", at);
    let depth = 0;
    let i = openAt;
    for (; i < CSS.length; i++) {
      if (CSS[i] === "{") depth++;
      else if (CSS[i] === "}") {
        depth--;
        if (depth === 0) break;
      }
    }
    blocks.push(CSS.slice(openAt + 1, i));
    searchFrom = i + 1;
  }
  expect(blocks.length).toBeGreaterThan(0);
  return blocks.join("\n");
}

// --- Rung 1 (full) -> Rung 2 (count only): the DETAIL goes first --------

test("rung 1->2 (480px): the detail — timing/caveat, the least actionable part — is what disappears, not the count", () => {
  const block = containerBlock(480);
  expect(block).toMatch(/\.listing-search-count-detail\s*\{[^}]*display:\s*none/);
  // The count's own class is untouched at this rung.
  expect(block).not.toMatch(/\.listing-search-count-base\s*\{[^}]*display:\s*none/);
});

test("rung 1->2 (480px): the wide-pin reservation shrinks to the plain has-pin value, freeing room for the query", () => {
  const block = containerBlock(480);
  // Plain (no clear button) wide-pin collapses to the same 116px the
  // non-wide .has-pin rule above reserves.
  expect(block).toMatch(
    /\.listing-search-box\.has-pin\.wide-pin:has\(\.listing-search-count\)\s*\.listing-search-input\s*\{\s*padding-right:\s*116px/,
  );
  // Crumb-slot wide-pin collapses to the crumb-slot plain has-pin value.
  expect(block).toMatch(
    /\.crumb-search-slot[\s\S]*?\.has-pin\.wide-pin:has\(\.listing-search-count\)[\s\S]*?padding-right:\s*126px/,
  );
});

// --- Rung 2 (count only) -> Rung 3 (nothing): the COUNT goes next -------

test("rung 2->3 (360px): both the count and the detail are hidden", () => {
  const block = containerBlock(360);
  expect(block).toMatch(
    /\.listing-search-count-base,?\s*\n?\s*\.listing-search-count-detail\s*\{[^}]*display:\s*none/,
  );
  // FINDING 2 (code review, 2026-09-10): this used to also assert
  // `.listing-search-shortcut-hint`'s own collapse lived in this SAME
  // 360px block — true only by coincidence, and a harmful one: the
  // shortcut button's wide/glyph switch (`boxWide`, SearchField.tsx) is
  // `HINT_WIDE_PX`, 340px, BELOW 360px, so sharing this number meant the
  // button's collapsed-glyph state (rendered whenever `boxWide` is false,
  // i.e. below 340px) was hidden by this very rule at every width it could
  // ever appear at — see search-shortcut-collapse.test.ts. The shortcut
  // hint now hides at its own, lower breakpoint instead of this one.
  expect(block).not.toMatch(/\.listing-search-shortcut-hint\s*\{[^}]*display:\s*none/);
});

test("rung 2->3 (360px): the pin's own reservation falls all the way back to the no-pin input padding, giving the freed space to the query", () => {
  const block = containerBlock(360);
  // Plain, no clear button: matches the resting `.listing-search .listing-search-input` padding (10px).
  expect(block).toMatch(
    /\.listing-search-box\.has-pin:has\(\.listing-search-count\)\s*\n?\s*\.listing-search-input,[\s\S]{0,200}padding-right:\s*10px/,
  );
  // With a clear button present: matches the resting has-clear:not(.has-pin) value (40px).
  expect(block).toMatch(/padding-right:\s*40px/);
});

test("rung 3: the pin's own element survives — it is a small hoverable target, not display:none — so its tooltip stays reachable", () => {
  const block = containerBlock(360);
  expect(block).not.toMatch(/\.listing-search-count\s*\{[^}]*display:\s*none/);
  expect(block).toMatch(/\.listing-search-count\s*\{\s*width:\s*\d+px;\s*height:\s*\d+px;\s*\}/);
});

// --- The tooltip carries the full, untruncated text no matter the rung --

test("the outer pin element's title/aria-label are set once, unconditionally, from the FULL sentence — not from the degradable base/detail split", () => {
  const at = SEARCH_FIELD.indexOf('className="listing-search-count"');
  expect(at).toBeGreaterThan(-1);
  const block = SEARCH_FIELD.slice(at, at + 400);
  expect(block).toMatch(/title=\{searchCountFull\}/);
  expect(block).toMatch(/aria-label=\{searchCountFull\}/);
  // The base/detail split lives INSIDE this element, as separate children —
  // degrading them visually never touches the title/aria-label above.
  expect(block).toMatch(/listing-search-count-base/);
  expect(block).toMatch(/listing-search-count-detail/);
});

// --- The count and its detail are genuinely separable, not a single string

test("Listing.tsx no longer bakes the caveat/latency into the count string itself — they are threaded as a separate prop", () => {
  expect(LISTING).toMatch(/searchCountDetail/);
  expect(LISTING).not.toMatch(/withCaveat\(/);
});

test("SearchField only renders the detail span when there is a detail to show", () => {
  const at = SEARCH_FIELD.indexOf('className="listing-search-count-detail"');
  expect(at).toBeGreaterThan(-1);
  const before = SEARCH_FIELD.slice(Math.max(0, at - 120), at);
  expect(before).toMatch(/searchCountDetail\s*!==\s*null\s*&&/);
});

// --- FINDING 1 (code review, 2026-09-10): a caveat with no count must still

test("the chip renders on a caveat alone — searchCount !== null is not the only gate", () => {
  const at = SEARCH_FIELD.indexOf('className="listing-search-count"');
  expect(at).toBeGreaterThan(-1);
  // The condition guarding the whole `<span className="listing-search-
  // count">` element sits just above it — assert on the text immediately
  // before the element, not on `searchCount !== null` in isolation, since
  // that alone is exactly the bug: a search that hasn't settled a count yet
  // ("indexing…", "building index… N files") or one whose only answer is
  // an error ("search failed") sets `searchCountDetail` with `searchCount`
  // still null, and dropping the whole chip in that case hid the one
  // signal saying the rows on screen don't (yet, or no longer) answer the
  // query.
  const before = SEARCH_FIELD.slice(Math.max(0, at - 2000), at);
  expect(before).toMatch(
    /searchCount\s*!==\s*null\s*\|\|\s*searchCountDetail\s*!==\s*null/,
  );
});

test("Listing.tsx's hasPin also reserves room for a caveat-only chip", () => {
  const at = LISTING.indexOf("const hasPin");
  expect(at).toBeGreaterThan(-1);
  const block = LISTING.slice(at, at + 300);
  expect(block).toMatch(/searchCountDetail\s*!==\s*null/);
});

// --- withCaveat is gone, not stranded — its only remaining reference was

test("withCaveat is deleted, not left as dead code behind its own test", () => {
  expect(LISTING).not.toMatch(/\bwithCaveat\b/);
  expect(SEARCH_FIELD).not.toMatch(/\bwithCaveat\b/);
});
