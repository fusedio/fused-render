// The listing table's responsive column shedding, guarded at the SOURCE.
//
// This is a layout property, and the frontend test setup has no DOM — so what
// is testable here is the invariant that produced the bug, not the geometry
// that showed it. The bug: a container query hid the MODIFIED column but left
// NAME width-less, and because the status/sentinel rows span all three columns
// (colSpan={3}) the hidden column survives as a real column under
// table-layout:fixed. WebKit then split the leftover between the two
// width-less columns, so NAME took half and the shed column kept the rest as a
// blank strip running to the preview-pane divider.
//
// Every shed step must therefore pin NAME's width. That is exactly the kind of
// pairing a later edit drops, which is why it is asserted rather than trusted.
// Real geometry still needs a browser (see the report).
import { expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const CSS = readFileSync(join(import.meta.dir, "../../../styles/explorer.css"), "utf8");

/** The body of every `@container (...)` block in the stylesheet. */
function containerBlocks(): { header: string; body: string }[] {
  const out: { header: string; body: string }[] = [];
  const re = /@container ([^{]*)\{/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(CSS)) !== null) {
    // Walk braces from the block's opening one so nested rules come along.
    let depth = 1;
    let i = re.lastIndex;
    for (; i < CSS.length && depth > 0; i++) {
      if (CSS[i] === "{") depth++;
      else if (CSS[i] === "}") depth--;
    }
    out.push({ header: m[1].trim(), body: CSS.slice(re.lastIndex, i - 1) });
  }
  return out;
}

const shedding = containerBlocks().filter((b) => /listing-table/.test(b.body));

test("the listing sheds columns in container queries at all", () => {
  // If this fails the rest of the file is asserting nothing.
  expect(shedding.length).toBeGreaterThanOrEqual(2);
});

test("every block that hides a listing column also pins the name width", () => {
  for (const block of shedding) {
    const hides = /th\.col-(mtime|size|name)[^{]*\{[^}]*display:\s*none/s.test(block.body);
    if (!hides) continue;
    expect(block.body).toMatch(/th\.col-name[^{]*\{[^}]*width:/s);
  }
});

test("both documented shed steps are present and ordered widest-first", () => {
  // MODIFIED goes before SIZE: the narrower query has to come second so its
  // `width: 100%` for NAME wins on specificity ties.
  const widths = shedding.map((b) => {
    const m = b.header.match(/max-width:\s*(\d+)px/);
    return m ? Number(m[1]) : NaN;
  });
  expect(widths).toEqual([...widths].sort((a, b) => b - a));
});

test("the widest shed step keeps room for SIZE, the narrowest gives all to NAME", () => {
  const mtimeStep = shedding.find((b) => /th\.col-mtime[^{]*\{[^}]*display:\s*none/s.test(b.body));
  const sizeStep = shedding.find((b) => /th\.col-size[^{]*\{[^}]*display:\s*none/s.test(b.body));
  expect(mtimeStep).toBeDefined();
  expect(sizeStep).toBeDefined();
  // NAME + the still-present SIZE column must add up to the whole table.
  expect(mtimeStep!.body).toMatch(/th\.col-name[^{]*\{[^}]*width:\s*calc\(100%\s*-\s*96px\)/s);
  expect(sizeStep!.body).toMatch(/th\.col-name[^{]*\{[^}]*width:\s*100%/s);
});

test("the SIZE column's declared width is the one the calc() subtracts", () => {
  // The calc above hardcodes 96px; if the column is retuned and the calc is
  // not, the dead strip comes straight back.
  expect(CSS).toMatch(/table\.listing-table th\.col-size\s*\{\s*width:\s*96px/s);
});
