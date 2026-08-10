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
