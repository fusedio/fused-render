// The examples panel's cap has to live on the SURFACE (`.listing-completion`,
// which paints the background/border/shadow), not on the row inside it — a
// row is a block box inside that surface, and capping a child's width cannot
// shrink the parent around it. Same CSS-parsing pattern as
// search-bar-expand.test.ts: read explorer.css and Listing.tsx as text and
// assert on the selectors/declarations and the class names actually used,
// rather than rendering (there is no DOM in this suite).
import { expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const CSS = readFileSync(join(import.meta.dir, "../../../styles/explorer.css"), "utf8").replace(
  /\/\*[\s\S]*?\*\//g,
  "",
);
const LISTING = readFileSync(join(import.meta.dir, "../Listing.tsx"), "utf8");

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

test("the container's own class is capped in the 420-480px band, max-width not width", () => {
  const decls = rulesFor(".listing-completion.listing-completion-examples");
  expect(decls.length).toBeGreaterThan(0);
  const joined = decls.join("\n");
  expect(joined).not.toMatch(/(?<!max-)width:\s*\d/);
  const match = joined.match(/max-width:\s*(\d+)px/);
  expect(match).toBeTruthy();
  const px = Number(match![1]);
  expect(px).toBeGreaterThanOrEqual(420);
  expect(px).toBeLessThanOrEqual(480);
});

test("the row itself carries no max-width — an inert declaration once the container is capped", () => {
  const decls = rulesFor(".listing-completion-row.listing-completion-example");
  const joined = decls.join("\n");
  expect(joined).not.toMatch(/max-width/);
});

test("the completion dropdown's own surface stays uncapped — it lists real paths, not fixed strings", () => {
  // The plain `.listing-completion` rule (the shared surface every variant
  // paints itself with) must not itself gain a max-width — only the
  // `.listing-completion-examples` modifier combination may.
  const re = /([^{}]+)\{([^{}]*)\}/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(CSS)) !== null) {
    const selector = m[1].trim();
    if (selector === ".listing-completion") {
      expect(m[2]).not.toMatch(/max-width/);
    }
  }
});
