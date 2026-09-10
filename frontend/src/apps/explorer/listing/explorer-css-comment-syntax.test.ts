// THE DEFECT this guards (running-screen review, 2026-09-10): a `/* ... */`
// comment in explorer.css quoted an example glob, `~` + wildcard + `/` +
// wildcard + `.zip`, whose two adjacent characters closed the CSS comment
// early. Everything after it on the following lines then parsed as CSS
// declarations, and an apostrophe in ordinary prose ("isn't") opened a
// string that never closed — a build failure (vite: "Unterminated
// string") with no test failure, because every CSS test in this directory
// (search-mode-chip.test.ts, search-examples-width.test.ts,
// search-count-pin-degrade.test.ts) strips comments with a regex before
// asserting anything, and `tsc` never parses CSS at all. `bun run build`
// was the only thing that actually caught it.
//
// CSS comments do not nest and have no escape mechanism, so the fix is
// permanently fragile to the same mistake recurring in prose. This test
// runs the real CSS parser vite/tailwind uses (lightningcss) over the
// file directly — no DOM, no build step — so a comment that closes early
// and corrupts the declarations after it fails here exactly like it fails
// the real build.
import { expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { transform } from "lightningcss";

const CSS_PATH = join(import.meta.dir, "../../../styles/explorer.css");

test("explorer.css parses as valid CSS (no comment closes early)", () => {
  const code = readFileSync(CSS_PATH);
  expect(() => transform({ filename: "explorer.css", code, minify: false })).not.toThrow();
});
