// The Enter-to-open-and-search banner (Listing.tsx's old `searching &&
// !showsSearchHits && (!isPathQuery || pathQueryRefused)` branch, built from
// `enterPrompt`/`pathNotFoundMessage`) is GONE — SPEC-omnibox-search-
// affordance.md scope item 5 moves its coverage into SearchField.tsx's own
// completion dropdown instead of deleting it. This file used to assert the
// banner's own CSS wash; it now asserts two things instead: that the banner
// is actually gone (not just re-skinned), and that the dropdown row it
// became is driven by the same `searchAffordance` predicate
// (search-action-rows.ts) already covered directly by
// search-action-rows.test.ts. Same CSS/text-parsing pattern as
// search-bar-expand.test.ts — no DOM in this suite.
import { expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const CSS = readFileSync(join(import.meta.dir, "../../../styles/explorer.css"), "utf8").replace(
  /\/\*[\s\S]*?\*\//g,
  "",
);
const LISTING = readFileSync(join(import.meta.dir, "../Listing.tsx"), "utf8");
const SEARCH_FIELD = readFileSync(join(import.meta.dir, "../SearchField.tsx"), "utf8");

function rulesFor(selectorExact: string): string[] {
  const out: string[] = [];
  const re = /([^{}]+)\{([^{}]*)\}/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(CSS)) !== null) {
    if (m[1].trim() === selectorExact) out.push(m[2]);
  }
  return out;
}

test("the banner row is gone from Listing.tsx — no gate, no modifier class, no accent wash left behind", () => {
  expect(LISTING).not.toMatch(/listing-enter-row/);
  expect(LISTING).not.toMatch(/pathQueryRefused/);
  expect(LISTING).not.toMatch(/enterPrompt\(/);
  // `pathNotFoundMessage` still exists (enter-prompt.ts, enter-prompt.test.ts
  // cover it directly) — it just isn't CALLED from Listing.tsx any more.
  expect(LISTING).not.toMatch(/pathNotFoundMessage\(/);
  expect(rulesFor(".status-message.listing-enter-row").length).toBe(0);
});

test("neither enterPrompt nor the old banner's own gate survives as dead weight in Listing.tsx's imports", () => {
  expect(LISTING).not.toMatch(
    /import \{ enterPrompt, pathNotFoundMessage \} from "@apps\/explorer\/listing\/enter-prompt";/,
  );
  expect(LISTING).not.toMatch(/gateOpen/);
});

// The coverage moved to SearchField.tsx: it reads `searchAffordance` off
// the same `isPathQuery`/`typedAddress`/`searching` the removed banner used
// to gate on, not a second, parallel predicate.
test("SearchField.tsx drives the dropdown's search offer off searchAffordance, not a re-derived gate", () => {
  expect(SEARCH_FIELD).toMatch(
    /import \{ searchAffordance, type SearchActionRow \} from "@apps\/explorer\/listing\/search-action-rows";/,
  );
  const at = SEARCH_FIELD.indexOf("const affordance = searchAffordance(");
  expect(at).toBeGreaterThan(-1);
  const call = SEARCH_FIELD.slice(at, SEARCH_FIELD.indexOf(";", at));
  expect(call).toMatch(
    /searchAffordance\(query, isPathQuery, typedAddress, searching, escapes, pristine\)/,
  );
});

// pathNotFoundMessage's own text is unchanged (enter-prompt.test.ts covers
// it) — this just confirms the dropdown row actually reuses it rather than
// a rewritten string, the one thing search-action-rows.test.ts (a pure-
// function suite) can't see: that the TEXT ON SCREEN is `affordance.notice`.
test("the dropdown's not-found line is affordance.notice, not a second copy of the wording", () => {
  const at = SEARCH_FIELD.indexOf("affordance.notice &&");
  expect(at).toBeGreaterThan(-1);
  const block = SEARCH_FIELD.slice(at, at + 200);
  expect(block).toMatch(/\{affordance\.notice\}/);
});
