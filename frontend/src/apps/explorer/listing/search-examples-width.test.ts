// The dropdown's own cap has to live on the SURFACE (`.listing-completion`,
// which paints the background/border/shadow), not on a row inside it — a
// row is a block box inside that surface, and capping a child's width
// cannot shrink the parent around it. Same CSS-parsing pattern as
// search-bar-expand.test.ts: read explorer.css and SearchField.tsx as text
// and assert on the selectors/declarations and the class names actually
// used, rather than rendering (there is no DOM in this suite).
//
// ITEM 4 (running-screen review, 2026-09-10): the cap used to live only on
// `.listing-completion.listing-completion-examples` — the examples panel's
// own modifier — because that was the one surface anyone had noticed
// stretching to the full 1100px+ search field for a few hundred pixels of
// real content. The same defect was just as true of the folder-completion
// dropdown (a `Downloads`/`folder` pair with ~600px of nothing between
// them), so the cap moved onto the plain `.listing-completion` rule that
// EVERY variant already paints its surface with, and the now-redundant
// examples-only rule was deleted rather than kept as a second cap that
// could drift from the first.
import { expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const CSS = readFileSync(join(import.meta.dir, "../../../styles/explorer.css"), "utf8").replace(
  /\/\*[\s\S]*?\*\//g,
  "",
);
const LISTING = readFileSync(join(import.meta.dir, "../SearchField.tsx"), "utf8");

/** Every declaration block for a given selector fragment. */
function rulesFor(fragment: string): string[] {
  const out: string[] = [];
  const re = /([^{}]+)\{([^{}]*)\}/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(CSS)) !== null) {
    if (m[1].trim().includes(fragment)) out.push(m[2]);
  }
  return out;
}

test("the examples container carries its own modifier class, sibling to the completion dropdown's", () => {
  const at = LISTING.indexOf("listing-completion-examples");
  expect(at).toBeGreaterThan(-1);
  const before = LISTING.slice(Math.max(0, at - 40), at);
  expect(before).toMatch(/className="listing-completion\s*$/);
});

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

test("the examples modifier no longer carries its own, now-redundant max-width", () => {
  const decls = rulesFor(".listing-completion.listing-completion-examples");
  expect(decls.length).toBe(0);
});

test("the row itself carries no max-width — an inert declaration once the shared surface is capped", () => {
  const decls = rulesFor(".listing-completion-row.listing-completion-example");
  const joined = decls.join("\n");
  expect(joined).not.toMatch(/max-width/);
});
