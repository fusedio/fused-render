// THE APP PAGE'S GIT PEEK LAYOUT, read off the stylesheet — the same
// stylesheet-test reasoning `notifications-width.test.ts` argues at length:
// this suite runs under `react-test-renderer` with no CSSOM, so
// `getComputedStyle` has nothing to answer with, and each regression here
// lived entirely in the stylesheet's own numbers, not in any component's
// markup. Three code-review findings, confirmed live on the running page
// before this file existed:
//
//   1. `.app-page-split` had no `overflow: hidden`, so the parked peek (an
//      always-rendered, `transform: translateX(100%)`-when-shut box) grew a
//      horizontal page scrollbar on every app page even when git was never
//      opened. MEASURED: `document.documentElement.scrollWidth` 1807 vs
//      `clientWidth` 1390 with the peek shut.
//   5. `.app-page-frame-slot` had no width transition, so the page snapped
//      while `.app-git-peek` slid — MEASURED `transition-duration: 0s` on the
//      slot against the peek's own `0.2s`.
//   7. On a narrow window (`clampSideWidth`, apps/explorer/lib/side-width.ts,
//      leaves the width UNCHANGED once the container can't hold both floors
//      — below ~700px) the peek's JS-computed width could reach the whole
//      container, since neither side had a CSS floor of its own to fall back
//      on. Below ~380px the app behind it went to 0px wide.
import { expect, test } from "bun:test";
import { readFileSync } from "node:fs";

const RAW = readFileSync(new URL("./app-page.css", import.meta.url), "utf8");
const CSS = RAW.replace(/\/\*[\s\S]*?\*\//g, "");

/** The declarations of the rule whose selector list contains `selector` as a
 *  WHOLE selector, whitespace flattened — see notifications-width.test.ts's
 *  own copy of this helper for why exact rather than substring. */
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

test("the split clips the parked peek instead of growing a page scrollbar", () => {
  expect(block(".app-page-split")).toContain("overflow: hidden");
});

test("the frame slot's width runs off the same clock as the peek's own slide", () => {
  const rule = block(".app-page-frame-slot");
  expect(rule).toContain("transition: width var(--peek-dur) var(--peek-ease)");
});

test("the frame slot has a floor a narrow window cannot crush past", () => {
  expect(block(".app-page-frame-slot")).toContain("min-width: 320px");
});

test("the peek is capped so the app behind it always keeps that same floor", () => {
  expect(block(".app-git-peek")).toContain("max-width: calc(100% - 320px)");
});
