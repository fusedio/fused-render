// The one decision in lib/nav-history that is a policy rather than a delegation:
// when the ‹ › arrows are allowed to look dead.
//
// Everything else in that file hands straight to the platform — `history.back()`
// is `history.back()` — but "can I go back?" has no answer on the classic
// History API, so the module asks the Navigation API and has to say something
// when the engine does not have one. The rule is UNKNOWN MEANS ENABLED, and it
// is asymmetric on purpose: a live button that no-ops costs one click, while a
// greyed-out button that was wrong teaches the user the control does not work.
//
// The three shapes below are the three real engines this ships to — Chromium
// with the API, WebKit without it (the menubar pin's WKWebView), and the
// half-implementation that has `navigation` but not these getters, which is what
// makes the check a `typeof` rather than a truthiness test. Reading a missing
// getter as `false` would disable both arrows on exactly the engine that told us
// least.
import { expect, test } from "bun:test";
import { navReachOf } from "@platform/lib/nav-history";

test("a Navigation API is believed in both directions", () => {
  expect(navReachOf({ canGoBack: true, canGoForward: false })).toEqual({
    back: true,
    forward: false,
  });
  expect(navReachOf({ canGoBack: false, canGoForward: true })).toEqual({
    back: false,
    forward: true,
  });
});

test("no Navigation API leaves both arrows live", () => {
  // WebKit. The alternative — disabling what we cannot verify — would ship an
  // explorer whose back button is permanently grey in the pinned popover.
  expect(navReachOf(undefined)).toEqual({ back: true, forward: true });
  expect(navReachOf(null)).toEqual({ back: true, forward: true });
});

test("a present API with absent getters is unknown, not false", () => {
  // `{}` and a partial are the same case: only a real boolean may disable an
  // arrow, so an `undefined` getter falls through to enabled rather than being
  // coerced.
  expect(navReachOf({})).toEqual({ back: true, forward: true });
  expect(navReachOf({ canGoBack: false })).toEqual({ back: false, forward: true });
});
