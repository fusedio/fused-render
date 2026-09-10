// THE FLOATING COLUMN IS ONE SHARED WIDTH, read off the stylesheet.
//
// User: "why are some notification cards significantly wider than others? I
// want all of them to have the same width." The bug was never in any one
// card's own markup — `.notif-host` only ever set a `max-width` (a ceiling),
// paired with `align-items: flex-end` (which shrink-wraps each child to its
// own content underneath that ceiling), so a short caption sat at `.dl-row`'s
// 238px floor while a long title stretched toward the 360px ceiling and the
// stack read as a ragged pile.
//
// A stylesheet test rather than a computed-style one, the same reasoning
// `apps/claude/styles/refusal.test.ts` and `parity.test.ts` already argue at
// length: this suite runs under `react-test-renderer` with no CSSOM, so
// `getComputedStyle` has nothing to answer with, and the regression itself
// lived entirely in the stylesheet's own numbers, not in any component's
// markup. The visual half — a very short caption and a very long clamped
// title producing the same card width — is checked by eye in the browser.
import { expect, test } from "bun:test";
import { readFileSync } from "node:fs";

const RAW = readFileSync(new URL("./notifications.css", import.meta.url), "utf8");
/** Comments out, so a selector mentioned in prose is not mistaken for a rule —
 *  this file's own comments quote `.notif-host`, `.dl-row`, etc. at length. */
const CSS = RAW.replace(/\/\*[\s\S]*?\*\//g, "");

/** The declarations of the rule whose selector list contains `selector` as a
 *  WHOLE selector, whitespace flattened. Exact rather than substring: several
 *  selectors here are prefixes of a longer one in the same file (`.notif-host`
 *  of `.notif-host > *`, `.toast-slot` of `.toast-slot > .dl-row`), so a
 *  substring match would silently read the wrong rule's values. */
function block(selector: string): string {
  const found: string[] = [];
  const re = /([^{}]+)\{([^{}]*)\}/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(CSS))) {
    const parts = m[1]!.split(",").map((x) => x.replace(/\s+/g, " ").trim());
    if (parts.includes(selector)) found.push(m[2]!.replace(/\s+/g, " ").trim());
  }
  expect(found.length, "no rule for " + selector).toBe(1);
  return found[0]!;
}

test("the column sets a real width, not only a cap, and stretches its entries to it", () => {
  const rule = block(".notif-host");
  expect(rule).toContain("width: min(360px, calc(100vw - 32px))");
  expect(rule).toContain("align-items: stretch");
  // Not the old shrink-wrap behaviour that caused the bug in the first place.
  expect(rule).not.toContain("align-items: flex-end");
});

test("every entry slot fills the column's width rather than only capping under it", () => {
  const rule = block(".toast-slot");
  expect(rule).toContain("width: 100%");
});

test("the column still declines pointer events over its own empty region", () => {
  // Unit D must not disturb the click-through split: the column itself stays
  // click-through, each entry (a direct child) takes clicks back.
  expect(block(".notif-host")).toContain("pointer-events: none");
  expect(block(".notif-host > *")).toContain("pointer-events: auto");
});
