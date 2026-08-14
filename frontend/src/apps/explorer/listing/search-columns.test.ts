// Search mode renders ONE column — the path — and everything the table does
// per-column has to follow the mode. Guarded at the SOURCE for the same reason
// column-shedding.test.ts is: this is table layout, and the frontend suite has
// no DOM, so what is testable here is the mechanism.
//
// Why it needs guarding at all:
//
//   1. A status row spanning colSpan={3} under a one-column <thead> declares
//      two columns nothing else mentions. Under table-layout:fixed the engine
//      then has width-less columns to distribute the remainder to — the exact
//      dead-strip failure column-shedding.test.ts documents, arrived at from
//      the other direction. Every colSpan therefore goes through columnCount().
//   2. The Size/Modified headers were the only control for sorting search
//      results, and search-result sorting is gone with them: a capped,
//      partially-streamed hit set sorted by size or date is a confident answer
//      to a question the data cannot answer. Relevance order, full stop — so
//      the surviving header must not look or behave like a sort control, and
//      no searchSort machinery may survive to re-order hits invisibly.
import { expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { columnCount } from "@apps/explorer/listing/types";
import * as search from "@apps/explorer/listing/search";

const LISTING = readFileSync(join(import.meta.dir, "../Listing.tsx"), "utf8");
const HOOK = readFileSync(join(import.meta.dir, "useWalkSearch.ts"), "utf8");
const CSS = readFileSync(join(import.meta.dir, "../../../styles/explorer.css"), "utf8");

/** The `if (searching) { … }` arm of the table-body builder. */
function searchBody(): string {
  const start = LISTING.indexOf("\n  if (searching) {");
  const end = LISTING.indexOf('\n  } else if (state.status === "loading") {', start);
  expect(start).toBeGreaterThan(0);
  expect(end).toBeGreaterThan(start);
  return LISTING.slice(start, end);
}

/** Everything else in the builder — the plain listing's rows and status rows. */
function listingBody(): string {
  const body = searchBody();
  return LISTING.replace(body, "");
}

function thead(): string {
  const start = LISTING.indexOf("<thead className=");
  const end = LISTING.indexOf("</thead>", start);
  expect(start).toBeGreaterThan(0);
  return LISTING.slice(start, end);
}

test("the search listing is one column and the plain listing is three", () => {
  expect(columnCount(true)).toBe(1);
  expect(columnCount(false)).toBe(3);
});

test("no status or banner row hardcodes a column count", () => {
  // A literal survives a mode change silently; columnCount cannot.
  expect(LISTING).not.toMatch(/colSpan=\{\s*\d/);
  expect(LISTING).toMatch(/colSpan=\{cols\}/);
});

test("search-hit rows render no size or modified cell", () => {
  const body = searchBody();
  expect(body).toMatch(/className="search-path"/); // the path IS the row
  expect(body).not.toMatch(/<td className="size"/);
  expect(body).not.toMatch(/<td className="mtime"/);
});

test("the plain listing keeps all three cells", () => {
  const body = listingBody();
  expect(body).toMatch(/<td className="size"/);
  expect(body).toMatch(/<td className="mtime"/);
});

test("the search header is not a sort control", () => {
  // No click target, no arrow, no `sortable` — there is nothing to sort by.
  const head = thead();
  const searchArm = head.slice(head.indexOf("searching ?"), head.indexOf(") : ("));
  expect(searchArm.length).toBeGreaterThan(0);
  expect(searchArm).not.toMatch(/sortable/);
  expect(searchArm).not.toMatch(/onClick/);
  expect(searchArm).not.toMatch(/sort-arrow/);
  expect(searchArm).toMatch(/col-name/); // still the width-less NAME column
});

test("no searchSort machinery survives anywhere", () => {
  expect(LISTING).not.toMatch(/searchSort/);
  expect(HOOK).not.toMatch(/searchSort/);
  // sortHits existed only to apply it; an unused export invites a caller back.
  expect("sortHits" in search).toBe(false);
});

test("only a sortable header advertises itself as clickable", () => {
  // `cursor: pointer` on the bare `th` promised a sort the search header does
  // not perform. The rule has to be scoped to the sortable ones.
  const bare = CSS.match(/table\.listing-table th \{([^}]*)\}/);
  expect(bare).not.toBeNull();
  expect(bare![1]).not.toMatch(/cursor:\s*pointer/);
  expect(CSS).toMatch(/th\.sortable[^{]*\{[^}]*cursor:\s*pointer/s);
});
