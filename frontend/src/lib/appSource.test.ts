// The badge an app card shows for its listing source (D205, D207). Small, but
// it is the only thing distinguishing three sources that otherwise render
// identically — and it used to be an inline `=== "claude-science"` written out
// separately in AppPreviewCard and in Home's RecentRow, which is exactly the
// shape that let those two disagree about opening once already.
import { expect, test } from "bun:test";

import { APP_SOURCE_LABELS, appSourceLabel, type AppSource } from "./api";

test("only non-workspace sources are badged", () => {
  // The workspace is the default and the overwhelming majority; labelling it
  // would put a badge on nearly every card and so distinguish nothing.
  expect(appSourceLabel("workspace")).toBe(null);
  // An older backend sends no source at all, which can only mean workspace.
  expect(appSourceLabel(undefined)).toBe(null);

  expect(appSourceLabel("claude-science")).toBe("Claude Science");
  expect(appSourceLabel("claude-code")).toBe("Claude Code");
});

test("every non-workspace source in the union has a label", () => {
  // The point of keying the record off Exclude<AppSource, "workspace"> is that
  // adding a source without a label is a type error rather than a silently
  // unlabelled card. This asserts the runtime half of that: no entry may be
  // missing or blank.
  for (const [source, label] of Object.entries(APP_SOURCE_LABELS)) {
    expect(label.length).toBeGreaterThan(0);
    expect(appSourceLabel(source as AppSource)).toBe(label);
  }
});

test("an unknown source from a newer backend is unbadged, not crashed", () => {
  // Forward compatibility: an older shell against a server that grew a fourth
  // source shows the card with no badge rather than "undefined".
  expect(appSourceLabel("something-new" as AppSource)).toBe(null);
});
