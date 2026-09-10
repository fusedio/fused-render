// The zero-match offer row ("No matches for X. Search Y instead") renders
// inside table.listing-table using the shared `status-message` row shape —
// same as "Searching…" and the capped-away count above it. That shared `td`
// rule (table.listing-table td) sets `white-space: nowrap` for every real,
// single-line cell; left un-overridden here, the offer's longer sentence
// ellipsizes at a narrow pane width instead of wrapping onto a second line,
// degrading back into the dead end the offer exists to fix.
import { expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const CSS = readFileSync(join(import.meta.dir, "../../../styles/explorer.css"), "utf8").replace(
  /\/\*[\s\S]*?\*\//g,
  "",
);

function rulesFor(selectorExact: string): string[] {
  const out: string[] = [];
  const re = /([^{}]+)\{([^{}]*)\}/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(CSS)) !== null) {
    if (m[1].trim() === selectorExact) out.push(m[2]);
  }
  return out;
}

test("the listing table's own status-message row wraps rather than clipping", () => {
  const rules = rulesFor("table.listing-table td.status-message");
  expect(rules.length).toBeGreaterThan(0);
  expect(rules.some((r) => /white-space:\s*normal/.test(r))).toBe(true);
});
