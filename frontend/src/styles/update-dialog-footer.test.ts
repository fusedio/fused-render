// THE UPDATE DIALOG'S FOOTER IS THE SAME HEIGHT IN EVERY STAGE, read off the
// stylesheets.
//
// This is the one promise the restart flow makes about its own chrome, and it is
// not decorative: the dialog has no ✕, no Esc and no backdrop, so a reader is
// pinned in front of it for the whole restart — and the footer swaps a button
// for a three-step strip and back again underneath their cursor while they are.
// A footer that changed height would move the dialog (it is centred) and move
// the button on `gave-up` out from under the pointer aimed at it.
//
// Three rules hold it, and all three are numbers in CSS that no component test
// can see:
//   1. the strip is exactly `.btn`'s 32px box;
//   2. the strip's own state modifiers (`is-back`, `is-stalled`) change colour
//      and nothing else — a tint that added padding would grow the row;
//   3. `gave-up`, the one stage showing BOTH, lays them out side by side
//      (`margin-right: auto`, the `.modal-dirty-hint` grammar) rather than
//      stacking them.
//
// A stylesheet test rather than a computed-style one, for the reason
// `notifications-width.test.ts` argues at length: this suite runs under
// `react-test-renderer` with no CSSOM, and the regression being guarded lives
// entirely in the stylesheets' numbers.
import { expect, test } from "bun:test";
import { readFileSync } from "node:fs";

/** Comments out, so a selector quoted in prose is not mistaken for a rule —
 *  these files quote `.update-dialog-steps` and `.btn` at length. */
const strip = (url: URL) => readFileSync(url, "utf8").replace(/\/\*[\s\S]*?\*\//g, "");
const NOTIFICATIONS = strip(new URL("./notifications.css", import.meta.url));
const BUTTONS = strip(new URL("./buttons-modal.css", import.meta.url));

/** The declarations of the rule whose selector list contains `selector` as a
 *  WHOLE selector, whitespace flattened. Exact rather than substring: several
 *  selectors here are prefixes of longer ones in the same file
 *  (`.update-dialog-steps` of `.update-dialog-steps-link`), so a substring match
 *  would silently read the wrong rule's values. */
function block(css: string, selector: string): string {
  const found: string[] = [];
  const re = /([^{}]+)\{([^{}]*)\}/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(css))) {
    const parts = m[1]!.split(",").map((x) => x.replace(/\s+/g, " ").trim());
    if (parts.includes(selector)) found.push(m[2]!.replace(/\s+/g, " ").trim());
  }
  expect(found.length, "no rule for " + selector).toBe(1);
  return found[0]!;
}

/** Every rule whose selector list mentions `selector` as a whole selector or as
 *  the subject of a compound one (`.x.is-back`, `.x .y`) — how a state modifier
 *  is found without knowing how it was spelled. */
function rulesTouching(css: string, selector: string): string[] {
  const out: string[] = [];
  const re = /([^{}]+)\{([^{}]*)\}/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(css))) {
    const sel = m[1]!.replace(/\s+/g, " ").trim();
    if (sel.includes(selector)) out.push(m[2]!.replace(/\s+/g, " ").trim());
  }
  return out;
}

test("the strip is exactly the box the button it replaces occupied", () => {
  const steps = block(NOTIFICATIONS, ".update-dialog-steps");
  const btn = block(BUTTONS, ".btn");
  expect(btn).toContain("height: 32px");
  expect(steps).toContain("height: 32px");
  // The type matches too, so the two never sit at different optical weights in
  // the same row on `gave-up`.
  expect(btn).toContain("font-size: 13px");
  expect(steps).toContain("font-size: 13px");
});

test("no state of the strip changes its height", () => {
  // `is-back` tints and `is-stalled` greys. Either one adding padding, a border
  // or a height would resize the dialog at the exact moment — the last beat
  // before a reload, or the moment a failed restart offers its button — when it
  // matters most that it does not.
  for (const rule of rulesTouching(NOTIFICATIONS, ".update-dialog-steps.is-")) {
    expect(rule).not.toContain("height");
    expect(rule).not.toContain("padding");
    expect(rule).not.toContain("border-width");
    expect(rule).not.toContain("border:");
  }
  // The padding is set ONCE, on the base rule, so the tint has a box to sit in
  // without the words moving sideways when it arrives.
  expect(block(NOTIFICATIONS, ".update-dialog-steps")).toContain("padding: 0 8px");
});

test("the strip and the button share one row rather than stacking", () => {
  // `gave-up` is the only stage that draws both. The footer is a flex row
  // (buttons-modal.css) with `justify-content: flex-end`, so the strip claims
  // the left the way `.modal-dirty-hint` does — the same grammar, and the only
  // arrangement that does not add a second 32px line to the footer.
  expect(block(NOTIFICATIONS, ".update-dialog-steps")).toContain("margin-right: auto");
  expect(block(BUTTONS, ".modal-dirty-hint")).toContain("margin-right: auto");
  expect(block(BUTTONS, ".modal-footer")).toContain("justify-content: flex-end");
});

test("the three marks share one box, so a step completing does not shuffle the words", () => {
  // Tick, spinner and hollow dot are different glyphs in the same 14px slot —
  // `.update-spinner`'s own diameter, since it is one of the three.
  const mark = block(NOTIFICATIONS, ".update-dialog-step-mark");
  expect(mark).toContain("width: 14px");
  expect(mark).toContain("height: 14px");
  expect(mark).toContain("flex: 0 0 14px");
  const spinner = strip(new URL("./sidebar.css", import.meta.url));
  expect(block(spinner, ".update-spinner")).toContain("width: 14px");
});

test("the success tint is the app's own token, not a colour invented here", () => {
  const back = block(NOTIFICATIONS, ".update-dialog-steps.is-back");
  expect(back).toContain("var(--success)");
  expect(back).not.toMatch(/#[0-9a-fA-F]{3,8}/);
  expect(back).not.toMatch(/\brgb a?\(/);
});

test("the word slot is sized by a hidden ghost, and the joiner is fixed, so no mark slides", () => {
  // The strip is shrink-to-fit with its slack OUTSIDE it (`margin-right: auto`),
  // so a flexible connector could never absorb a label growing from "Restart"
  // to "Restarting…". What holds the geometry is the word slot: ghost and text
  // stacked in one grid cell, the ghost invisible but still taking width.
  const word = block(NOTIFICATIONS, ".update-dialog-step-word");
  expect(word).toContain("display: inline-grid");
  expect(block(NOTIFICATIONS, ".update-dialog-step-word > *")).toContain("grid-area: 1 / 1");
  expect(block(NOTIFICATIONS, ".update-dialog-step-ghost")).toContain("visibility: hidden");
  const link = block(NOTIFICATIONS, ".update-dialog-steps-link");
  expect(link).toMatch(/flex: 0 0 \d+px/);
  expect(link).not.toContain("flex: 1");
});

test("the body reserves two lines, so the shorter sentences do not lift the footer", () => {
  // The in-flight sentence wraps to two lines at the modal's width; the 25s
  // withdrawal, `back` and `ready` are one. Without the floor the footer jumps
  // up by a line at the 25s mark, under a reader who cannot dismiss the dialog.
  expect(block(NOTIFICATIONS, ".update-dialog-body")).toContain("min-height: 2lh");
});
