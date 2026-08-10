// The preview pane's focus contract, as arithmetic: the signal the shell puts
// on a preview's URL, and the one question the guard asks before it takes
// keyboard focus back off a frame.
//
// Repo memory is explicit that a headless test cannot see focus any more than
// it can see layout — so the wiring is kept to "ask this, then blur", and what
// is testable is exactly this: the param round-trips, and the reclaim rule.
import { describe, expect, test } from "bun:test";
import {
  NO_FOCUS_PARAM,
  noFocusRequested,
  shouldReclaimFocus,
  tabEntersFrame,
  withNoFocus,
} from "./frame-focus";

describe("the `_nofocus` signal", () => {
  test("is added to a src that already has a query", () => {
    expect(withNoFocus("/render?path=%2Fw%2Fa.html")).toBe(
      "/render?path=%2Fw%2Fa.html&_nofocus=1",
    );
  });

  test("is added to a src that has none", () => {
    expect(withNoFocus("/render")).toBe("/render?_nofocus=1");
  });

  test("is never added twice", () => {
    // The pane rebuilds the src on every render; a param that accumulated
    // would grow the URL (and, worse, make the frame reload on a re-render).
    const once = withNoFocus("/render?path=x");
    expect(withNoFocus(once)).toBe(once);
  });

  test("round-trips through the reader the template side mirrors", () => {
    // runtime.js reads the same param out of its own URL. The two spellings
    // must agree, which is why the name is a constant and not a literal.
    expect(noFocusRequested(withNoFocus("/render?path=x").split("?")[1])).toBe(true);
    expect(NO_FOCUS_PARAM).toBe("_nofocus");
  });

  test("an ordinary preview URL asks for nothing", () => {
    expect(noFocusRequested("path=%2Fw%2Fa.html&_file=%2Fw%2Fb")).toBe(false);
    expect(noFocusRequested("")).toBe(false);
    // Only the affirmative value counts: `_nofocus=0` is not a request.
    expect(noFocusRequested("_nofocus=0")).toBe(false);
  });

  test("reads a full search string with its leading ?", () => {
    expect(noFocusRequested("?path=x&_nofocus=1")).toBe(true);
  });
});

describe("shouldReclaimFocus", () => {
  test("a frame that took focus on its own gives it back", () => {
    // The bug this exists for: a template autofocuses an input on boot, the
    // iframe takes document focus, and the arrow keys stop moving the listing
    // selection — the user can no longer browse file to file.
    expect(shouldReclaimFocus(true, false)).toBe(true);
  });

  test("a frame the user clicked into keeps focus", () => {
    // Focus must still transfer on a DELIBERATE act. Once the user has reached
    // into the pane, everything the template does with focus is theirs.
    expect(shouldReclaimFocus(true, true)).toBe(false);
  });

  test("focus that isn't in the frame is not the guard's business", () => {
    expect(shouldReclaimFocus(false, false)).toBe(false);
    expect(shouldReclaimFocus(false, true)).toBe(false);
  });
});

describe("tabEntersFrame", () => {
  // A shell's tab order, in document order: breadcrumb, the listing's search
  // box, then the pane — its header controls, then the frame itself.
  const CRUMB = "crumb";
  const SEARCH = "search";
  const MODE = "pane-mode-button";
  const FRAME = "frame";
  const stops = [CRUMB, SEARCH, MODE, FRAME];

  test("the Tab that is about to land in the frame releases it", () => {
    expect(tabEntersFrame(stops, MODE, FRAME, false)).toBe(true);
  });

  test("Shift+Tab back into the frame counts too", () => {
    // Something after the pane in the order — a footer control, the next pane.
    expect(tabEntersFrame([...stops, "after"], "after", FRAME, true)).toBe(true);
  });

  test("tabbing around the shell's own chrome does NOT", () => {
    // THE BUG. The release is one-shot and permanent (runtime.js), so a Tab
    // from the breadcrumb to the search box used to retire the preview's focus
    // suppression for good — nowhere near the pane, and the reader never asked
    // for the preview to have the keyboard.
    expect(tabEntersFrame(stops, CRUMB, FRAME, false)).toBe(false);
    expect(tabEntersFrame(stops, SEARCH, FRAME, true)).toBe(false);
    expect(tabEntersFrame(stops, SEARCH, FRAME, false)).toBe(false);
  });

  test("a Tab out of the frame is not a Tab into it", () => {
    expect(tabEntersFrame(stops, FRAME, FRAME, false)).toBe(false);
    expect(tabEntersFrame(stops, FRAME, FRAME, true)).toBe(false);
  });

  test("from nowhere (focus on <body>) Tab starts at the ends", () => {
    // The ordinary state after the guard has reclaimed: it blurs to <body>.
    expect(tabEntersFrame([FRAME, SEARCH], null, FRAME, false)).toBe(true);
    expect(tabEntersFrame(stops, null, FRAME, false)).toBe(false);
    expect(tabEntersFrame(stops, null, FRAME, true)).toBe(true);
  });

  test("a frame that is not a tab stop at all is never entered", () => {
    // The pane is gone (a narrow window, a row switch mid-keypress): nothing to
    // release, and nothing that could match the end-of-list cases above.
    expect(tabEntersFrame([CRUMB, SEARCH], SEARCH, FRAME, false)).toBe(false);
    expect(tabEntersFrame([], null, FRAME, false)).toBe(false);
  });
});
