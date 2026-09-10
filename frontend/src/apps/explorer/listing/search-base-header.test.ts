// The search-hits header used to say only "Path", with no answer to "path
// relative to what?" — the search field portaled into the crumb bar replaced
// the crumbs that used to answer that. Same CSS-parsing pattern as
// search-bar-expand.test.ts: read explorer.css and Listing.tsx as text, no
// DOM in this suite.
import { expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { contractHome } from "@apps/explorer/listing/home-path";

const CSS = readFileSync(join(import.meta.dir, "../../../styles/explorer.css"), "utf8").replace(
  /\/\*[\s\S]*?\*\//g,
  "",
);
const LISTING = readFileSync(join(import.meta.dir, "../Listing.tsx"), "utf8");

function rulesFor(selectorExact: string): string[] {
  const out: string[] = [];
  const re = /([^{}]+)\{([^{}]*)\}/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(CSS)) !== null) {
    if (m[1].trim() === selectorExact) out.push(m[2]);
  }
  return out;
}

test("the header names the base via the shared contraction helper, not a bare literal", () => {
  const at = LISTING.indexOf('<th className="col-name col-search-base"');
  expect(at).toBeGreaterThan(-1);
  const before = LISTING.slice(Math.max(0, at - 600), at);
  expect(before).toMatch(/Path in \$\{baseText\}/);
  expect(before).toMatch(/contractHome\(searchBase, home\)/);
});

test("the base is home-exact shows a lone ~, not the full home path", () => {
  const at = LISTING.indexOf('<th className="col-name col-search-base"');
  expect(at).toBeGreaterThan(-1);
  const before = LISTING.slice(Math.max(0, at - 600), at);
  expect(before).toMatch(/searchBase === home\s*\?\s*"~"\s*:\s*contractHome\(searchBase, home\)/);
});

test("the path segment is wrapped in its own element exempted from the header's uppercase transform", () => {
  const at = LISTING.indexOf('<th className="col-name col-search-base"');
  expect(at).toBeGreaterThan(-1);
  const tag = LISTING.slice(at, at + 600);
  expect(tag).toMatch(/col-search-base-path/);
  const decls = rulesFor("table.listing-table .col-search-base-path");
  expect(decls.length).toBe(1);
  expect(decls[0]).toMatch(/text-transform:\s*none/);
});

test("the title attribute carries the true, untransformed path", () => {
  const at = LISTING.indexOf('<th className="col-name col-search-base"');
  expect(at).toBeGreaterThan(-1);
  const tag = LISTING.slice(at, at + 400);
  expect(tag).toMatch(/title=\{baseLabel \|\| undefined\}/);
  // baseLabel is built from baseText, the same untransformed value rendered
  // in the exempted span — not a separately-cased copy.
  const before = LISTING.slice(Math.max(0, at - 600), at);
  expect(before).toMatch(/const baseLabel = searchBase \? `Path in \$\{baseText\}` : "";/);
});

test("falls back to the bare label when the base is empty or not yet known", () => {
  const at = LISTING.indexOf("baseLabel");
  expect(at).toBeGreaterThan(-1);
  const nearby = LISTING.slice(at, at + 500);
  expect(nearby).toMatch(/searchBase\s*\?\s*`Path in/);
  expect(nearby).toMatch(/"Path"/);
});

test("the header stays a plain <th>, not one of the sortable/sorted headers", () => {
  const at = LISTING.indexOf('<th className="col-name col-search-base"');
  expect(at).toBeGreaterThan(-1);
  const tag = LISTING.slice(at, at + 200);
  expect(tag).not.toMatch(/sortable/);
  expect(tag).not.toMatch(/onClick/);
});

test("long bases truncate with an ellipsis rather than wrapping the sticky header", () => {
  const decls = rulesFor("table.listing-table th.col-search-base");
  expect(decls.length).toBe(1);
  const decl = decls[0];
  expect(decl).toMatch(/text-overflow:\s*ellipsis/);
  expect(decl).toMatch(/white-space:\s*nowrap/);
  expect(decl).toMatch(/overflow:\s*hidden/);
});

test("the contraction helper matches the three existing inline copies' behavior", () => {
  expect(contractHome("/Users/dev/Downloads", "/Users/dev")).toBe("~/Downloads");
  expect(contractHome("/Users/dev", "/Users/dev")).toBe("/Users/dev");
  expect(contractHome("/var/log", "/Users/dev")).toBe("/var/log");
});
