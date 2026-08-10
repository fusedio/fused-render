// The listing table's responsive column shedding, guarded at the SOURCE.
//
// This is a layout property and the frontend test setup has no DOM, so what is
// testable here is the mechanism, not the geometry. Both are worth guarding
// because both have already been got wrong once:
//
//   1. `display: none` on the shed HEADER left two width-less columns (the
//      column survives either way — the status rows span all three via
//      colSpan={3}), and WebKit splits the remainder evenly between them. NAME
//      took half; the shed column kept the rest as a dead strip.
//   2. Pinning NAME with `calc(100% - 96px)` reads correctly and is silently
//      DISCARDED by WKWebKit for table cells under table-layout:fixed. It
//      passed a source check that asserted the calc string while the shipped
//      rule did nothing at all.
//
// So the invariant is: a shed column stays a RENDERED zero-width column, sized
// in px, leaving NAME as the only width-less column. Real geometry still needs
// a browser (see the report).
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

/** The declarations of one rule inside a block, by selector substring. */
function ruleFor(body: string, selector: string): string | null {
  const re = new RegExp(`[^{}]*${selector.replace(".", "\\.")}[^{]*\\{([^}]*)\\}`, "s");
  const m = body.match(re);
  return m ? m[1] : null;
}

const shedding = containerBlocks().filter((b) => /listing-table/.test(b.body));
const SHED = ["col-mtime", "col-size"];

test("the listing sheds columns in container queries at all", () => {
  // If this fails the rest of the file is asserting nothing.
  expect(shedding.length).toBeGreaterThanOrEqual(2);
});

test("a shed column's HEADER is never display:none", () => {
  // This is the original bug. A removed header contributes no width, which
  // leaves two width-less columns for WebKit to split down the middle.
  for (const block of shedding) {
    for (const col of SHED) {
      const decls = ruleFor(block.body, `th.${col}`);
      if (decls) expect(decls).not.toMatch(/display:\s*none/);
    }
  }
});

test("a shed column's header collapses to a zero px width with no box", () => {
  const collapsed = shedding.flatMap((block) =>
    SHED.map((col) => ruleFor(block.body, `th.${col}`)).filter((d): d is string => d !== null),
  );
  expect(collapsed.length).toBe(2); // MODIFIED and SIZE, one step each
  for (const decls of collapsed) {
    expect(decls).toMatch(/width:\s*0\b/);
    expect(decls).toMatch(/padding:\s*0\b/);
    // Content must not paint over the neighbouring column once the box is gone.
    expect(decls).toMatch(/font-size:\s*0\b/);
    expect(decls).toMatch(/overflow:\s*hidden/);
  }
});

test("the collapse rules outrank the default widths further down the file", () => {
  // The defaults (`table.listing-table th.col-size { width: 96px }`) sit AFTER
  // these blocks in source order, so equal specificity would lose. `thead` is
  // what wins the tie — dropping it silently restores the dead strip.
  for (const block of shedding) {
    for (const col of SHED) {
      if (!ruleFor(block.body, `th.${col}`)) continue;
      const selector = block.body.slice(0, block.body.indexOf(`th.${col}`) + col.length + 3);
      expect(selector).toMatch(new RegExp(`thead th\\.${col}`));
    }
  }
});

test("NAME is never given an explicit width, least of all a percentage calc", () => {
  // NAME must stay the single width-less column and take the remainder by
  // construction. calc() with a percentage is specifically discarded by
  // WKWebKit on table cells under fixed layout.
  for (const block of shedding) {
    expect(ruleFor(block.body, "th.col-name")).toBeNull();
  }
  expect(CSS).not.toMatch(/th\.col-name[^{]*\{[^}]*width:\s*calc\([^)]*%/s);
});

test("the cell CONTENT of a shed column is still removed from body rows", () => {
  // Only the header stays, and only to hold the column open at zero width.
  // MODIFIED and SIZE are the last two columns, so dropping their tds cannot
  // shift the surviving cells into the wrong columns.
  for (const col of ["mtime", "size"]) {
    const found = shedding.some((b) => {
      const decls = ruleFor(b.body, `td.${col}`);
      return decls !== null && /display:\s*none/.test(decls);
    });
    expect(found).toBe(true);
  }
});

test("both documented shed steps are present and ordered widest-first", () => {
  const widths = shedding.map((b) => {
    const m = b.header.match(/max-width:\s*(\d+)px/);
    return m ? Number(m[1]) : NaN;
  });
  expect(widths).toEqual([...widths].sort((a, b) => b - a));
});
