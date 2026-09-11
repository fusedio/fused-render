// THE REFUSAL FACES AND THE CURSOR VOCABULARY, read off the stylesheet.
//
// These are CSS facts with arguments behind them — T does not merely dim a
// disabled control, it picks each number and says why — and every one of them
// was measured wrong against :1777 (a blocked textarea at full opacity, a
// calendar reading as a generic disabled pill, a picture door offering
// `pointer` into a `zoom-in` viewer).
//
// A stylesheet test rather than a computed-style one, deliberately: the suite
// runs under `react-test-renderer` with no CSSOM, so `getComputedStyle` has
// nothing to answer with. What this CAN pin is that the rules exist with T's
// values — which is where all three regressions actually happened, since the
// markup and the `disabled` attribute were right the whole time. The visual
// half is checked in the browser against :1777.
import { expect, test } from "bun:test";
import { readFileSync } from "node:fs";

const RAW = readFileSync(new URL("./composer.css", import.meta.url), "utf8");
/** Comments out, so a selector mentioned in prose is not mistaken for a rule —
 *  this file's comments quote plenty of selectors. */
const CSS = RAW.replace(/\/\*[\s\S]*?\*\//g, "");

/** The declarations of the rule whose selector list contains `selector` as a
 *  WHOLE selector, whitespace flattened. Exact rather than substring: every
 *  selector here is a prefix of a longer one in the same file (`.c-chip-door`
 *  of `.c-chip-door:has(…)`, `.c-annchip` of `.c-annchip .c-txt`), so a
 *  substring match silently reads the wrong rule's values. */
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

test("the blocked textarea says it is shut (T:1673-1678)", () => {
  // T's argument: "the box itself says it is shut, since the banner is at the
  // other end of the pane and a reader who clicks into a dead textarea is owed
  // an answer there."
  const rule = block(".c-composer textarea:disabled");
  expect(rule).toContain("opacity: 0.6");
  expect(rule).toContain("cursor: not-allowed");
});

test("the blocked calendar has its OWN refusal, dimmer than the box (T:1826-1837)", () => {
  // Both numbers are argued at T's site: `not-allowed` because this and
  // `#box:disabled` "are the SAME refusal and should feel like one", and `.45`
  // rather than `.6` because "a 14px monochrome glyph at .6 still reads as a
  // live icon, where a whole greyed textarea does not".
  const rule = block(".c-schedbtn:disabled");
  expect(rule).toContain("opacity: 0.45");
  expect(rule).toContain("cursor: not-allowed");

  // And it is DIMMER than both the textarea and the generic pill it used to
  // inherit from — the pairing is the point, so the numbers are asserted
  // against each other rather than only against a literal.
  const pill = block(".c-pill:disabled");
  expect(pill).toContain("opacity: 0.55");
  const num = (r: string) => Number(/opacity:\s*([\d.]+)/.exec(r)![1]);
  expect(num(rule)).toBeLessThan(num(block(".c-composer textarea:disabled")));
  expect(num(rule)).toBeLessThan(num(pill));
});

test("the two refusals share one cursor, and the camera's does not (T:1834)", () => {
  // T is explicit that `.viewshot`'s disable is a different kind of refusal —
  // "a one-second capture that is nobody's mistake to make" — so it keeps
  // `default` while these two are `not-allowed`.
  expect(block(".c-composer textarea:disabled")).toContain("not-allowed");
  expect(block(".c-schedbtn:disabled")).toContain("not-allowed");
  expect(block(".c-pill:disabled")).toContain("cursor: default");
});

test("the chip's door covers the pill's own padding (PR2 deferred D9)", () => {
  // The full-pill door landed in P2-4, but with `padding: 0` it could not reach
  // the pill's padding — so the hit area was ~65% × 73% of a shape whose hover
  // state lit up all of it. The padding moved onto the door; the pill keeps
  // only the trailing side, where the ✕ (the door's sibling) sits.
  const door = block(".chat-root .c-annchip .c-chip-door");
  expect(door).toContain("padding: 3px 0 3px 10px");
  // A MATCHING NEGATIVE MARGIN, not a stripped pill: the pill's inset is shared
  // with the ANNOTATION chips in the same tray, which render no door at all —
  // taking it off them clipped their pin letter against the rounded edge
  // (Bugbot, PR #1074). The trailing side stays 0, because the ✕ lives there.
  expect(door).toContain("margin: -3px 0 -3px -10px");
  const pill = block(".chat-root .c-annchip");
  expect(pill).toContain("padding: 3px 6px 3px 10px");
});

test("a picture door is `zoom-in`, a glyph door is not, a refused door is neither (T:948, T:969)", () => {
  // The viewer this opens uses `zoom-in`/`zoom-out`, so a `pointer` here left
  // the cursor vocabulary disagreeing across the two ends of one gesture.
  expect(block(".chat-root .c-annchip .c-chip-door:has(.c-shotthumb)")).toContain(
    "cursor: zoom-in",
  );
  expect(block(".chat-root .c-annchip .c-chip-door")).toContain("cursor: pointer");
  expect(block(".chat-root .c-annchip .c-chip-door.is-inert")).toContain("cursor: default");

  // ORDER IS LOAD-BEARING: the `is-inert` rule and the `:has()` rule resolve to
  // the same specificity (`:has()` contributes its argument's), so the refusal
  // only wins by coming later. A refused picture chip must not offer a zoom
  // into a viewer with nothing behind it.
  expect(CSS.indexOf(".c-chip-door.is-inert")).toBeGreaterThan(
    CSS.indexOf(".c-chip-door:has(.c-shotthumb)"),
  );
});
