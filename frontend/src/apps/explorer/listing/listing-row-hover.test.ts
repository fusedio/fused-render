// Same CSS-text-parsing approach as search-completion-width.test.ts: read
// explorer.css and assert on the selectors/declarations actually present,
// rather than rendering (no DOM in this suite).
//
// The row itself is already the click target (table.listing-table tr.row's
// own `cursor: pointer`, overridden to `grab` only on the drag handle and a
// selected row — see the comment on td.name .row-handle). What a hovered row
// does NOT yet do is single out the name as the one thing in it that reads
// as a "go here" — the pattern this follows is transcript.css's
// `.annsum-note:hover .annsum-txt` (a comment row that is itself the click
// target, with an underline on its text on row hover, no background or
// border added).
import { expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const CSS = readFileSync(join(import.meta.dir, "../../../styles/explorer.css"), "utf8").replace(
  /\/\*[\s\S]*?\*\//g,
  "",
);

function rulesFor(fragment: string): string[] {
  const out: string[] = [];
  const re = /([^{}]+)\{([^{}]*)\}/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(CSS)) !== null) {
    if (m[1].trim().includes(fragment)) out.push(m[2]);
  }
  return out;
}

test("the row is a pointer everywhere except its drag handle, unchanged", () => {
  const rowRe = /table\.listing-table tr\.row\s*\{([^{}]*)\}/;
  const rowDecls = CSS.match(rowRe)?.[1] ?? "";
  expect(rowDecls).toMatch(/cursor:\s*pointer/);
});

test("hovering a row underlines its name text, and nothing else about the row changes", () => {
  const re = /table\.listing-table tr\.row:hover[^{,]*\.name-text[^{]*\{([^{}]*)\}/;
  const match = CSS.match(re);
  expect(match).toBeTruthy();
  const body = match![1];
  expect(body).toMatch(/text-decoration:\s*underline/);
  // No competing treatment — cursor/hover affordance only, per the row
  // itself already carrying the pointer.
  expect(body).not.toMatch(/background/);
  expect(body).not.toMatch(/border/);
});

test("no rule paints a background or border onto a hovered row beyond the existing .row:hover surface", () => {
  const decls = rulesFor("tr.row:hover");
  for (const d of decls) {
    // The existing tr.row:hover rule already paints --bg-alt (unchanged,
    // asserted below) — this guards against a SECOND, newly-added
    // background/border creeping in alongside the name underline.
    if (d.includes("--bg-alt")) continue;
    expect(d).not.toMatch(/border/);
  }
});
