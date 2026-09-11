// The dropdown's own cap has to live on the SURFACE (`.listing-completion`,
// which paints the background/border/shadow), not on a row inside it — a
// row is a block box inside that surface, and capping a child's width
// cannot shrink the parent around it. Same CSS-parsing pattern as
// search-bar-expand.test.ts: read explorer.css and SearchField.tsx as text
// and assert on the selectors/declarations and the class names actually
// used, rather than rendering (there is no DOM in this suite).
import { expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const CSS = readFileSync(join(import.meta.dir, "../../../styles/explorer.css"), "utf8").replace(
  /\/\*[\s\S]*?\*\//g,
  "",
);
const SEARCH_FIELD = readFileSync(join(import.meta.dir, "../SearchField.tsx"), "utf8");

/** Declaration blocks for an EXACT selector (not a substring match — several
 * selectors here share ".listing-completion" as a fragment). */
function rulesForExact(selector: string): string[] {
  const out: string[] = [];
  const re = /([^{}]+)\{([^{}]*)\}/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(CSS)) !== null) {
    if (m[1].trim() === selector) out.push(m[2]);
  }
  return out;
}

/** Declaration blocks for every selector whose comma-separated list includes
 * a variant built on `.listing-completion` — used below to make sure no
 * variant anywhere smuggles in a bare `width` the shared cap doesn't cover. */
function everyListingCompletionVariantDecl(): string[] {
  const out: string[] = [];
  const re = /([^{}]+)\{([^{}]*)\}/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(CSS)) !== null) {
    const selectors = m[1].split(",").map((s) => s.trim());
    if (selectors.some((s) => s.startsWith(".listing-completion"))) out.push(m[2]);
  }
  return out;
}

test("the shared surface itself is capped in the 420-480px band, max-width not width — every variant inherits it", () => {
  const decls = rulesForExact(".listing-completion");
  expect(decls.length).toBe(1);
  const joined = decls.join("\n");
  expect(joined).not.toMatch(/(?<!max-)width:\s*\d/);
  const match = joined.match(/max-width:\s*(\d+)px/);
  expect(match).toBeTruthy();
  const px = Number(match![1]);
  expect(px).toBeGreaterThanOrEqual(420);
  expect(px).toBeLessThanOrEqual(480);
});

test("the row class carries no max-width — an inert declaration once the shared surface is capped", () => {
  const decls = rulesForExact(".listing-completion-row");
  expect(decls.length).toBe(1);
  expect(decls[0]).not.toMatch(/max-width/);
});

test("no `.listing-completion` variant anywhere sets a bare width, not even `width: max-content` — the shared max-width cap is the only sizing rule any of them get", () => {
  for (const decls of everyListingCompletionVariantDecl()) {
    expect(decls).not.toMatch(/(?<![a-zA-Z-])width:\s*\S/);
  }
});

test("the deleted teaching-panel classes carry no CSS at all", () => {
  expect(CSS).not.toMatch(/\.listing-completion-examples/);
  expect(CSS).not.toMatch(/\.listing-completion-legend/);
});

test("the deleted teaching panel has no remaining trace in SearchField.tsx", () => {
  expect(SEARCH_FIELD).not.toMatch(/showSearchExamples|showExamples|listing-completion-examples|listing-completion-legend/);
});

test("the search button carries the grammar as an instant hint, not a native title", () => {
  const at = SEARCH_FIELD.indexOf("data-hint={SEARCH_GRAMMAR_HINT}");
  expect(at).toBeGreaterThan(-1);
  // The button this hint sits on has no `title` attribute — this app's
  // convention is `data-hint` XOR `title`, never both on the same element
  // (RecommendedCard.tsx's own doc comment states the reason: two native
  // titles on the same point would both fire).
  const buttonStart = SEARCH_FIELD.lastIndexOf("<button", at);
  const buttonEnd = SEARCH_FIELD.indexOf(">", at);
  const buttonTag = SEARCH_FIELD.slice(buttonStart, buttonEnd);
  expect(buttonTag).not.toMatch(/\btitle=/);
  expect(buttonTag).toMatch(/aria-label=/);
});
