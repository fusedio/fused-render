import { describe, expect, test } from "bun:test";
import { completionKeyAction, moveHighlight } from "@apps/explorer/listing/completion-keys";

describe("completionKeyAction", () => {
  test("Enter with no highlight falls through, dropdown or not", () => {
    // No dropdown showing at all.
    expect(completionKeyAction("Enter", false, -1, 0)).toEqual({
      type: "enter-passthrough",
    });
    // Dropdown showing, nothing arrowed to yet — this is the case that used
    // to accept row 0 by accident.
    expect(completionKeyAction("Enter", true, -1, 3)).toEqual({
      type: "enter-passthrough",
    });
  });

  test("Enter after an ArrowDown accepts the highlighted row", () => {
    expect(completionKeyAction("Enter", true, 0, 3)).toEqual({
      type: "enter-accept",
      index: 0,
    });
    expect(completionKeyAction("Enter", true, 2, 3)).toEqual({
      type: "enter-accept",
      index: 2,
    });
  });

  test("Tab accepts the first row with nothing highlighted", () => {
    expect(completionKeyAction("Tab", true, -1, 3)).toEqual({
      type: "tab-accept",
      index: 0,
    });
  });

  test("Tab accepts the highlighted row when one is arrowed to", () => {
    expect(completionKeyAction("Tab", true, 1, 3)).toEqual({
      type: "tab-accept",
      index: 1,
    });
  });

  test("Tab and Enter with nothing highlighted disagree on purpose", () => {
    const tab = completionKeyAction("Tab", true, -1, 3);
    const enter = completionKeyAction("Enter", true, -1, 3);
    expect(tab.type).toBe("tab-accept");
    expect(enter.type).toBe("enter-passthrough");
  });

  test("ArrowDown/ArrowUp move the highlight without touching the field", () => {
    expect(completionKeyAction("ArrowDown", true, 0, 3)).toEqual({
      type: "move",
      delta: 1,
    });
    expect(completionKeyAction("ArrowUp", true, 0, 3)).toEqual({
      type: "move",
      delta: -1,
    });
  });

  test("every key is inert with no dropdown showing, except Enter", () => {
    expect(completionKeyAction("ArrowDown", false, -1, 0)).toEqual({ type: "none" });
    expect(completionKeyAction("Tab", false, -1, 0)).toEqual({ type: "none" });
    expect(completionKeyAction("a", true, -1, 3)).toEqual({ type: "none" });
  });

  test("a dropdown with zero items behaves as not showing", () => {
    expect(completionKeyAction("Enter", true, -1, 0)).toEqual({
      type: "enter-passthrough",
    });
    expect(completionKeyAction("Tab", true, -1, 0)).toEqual({ type: "none" });
  });
});

describe("moveHighlight", () => {
  test("wraps forward past the last row to the first", () => {
    expect(moveHighlight(2, 1, 3)).toBe(0);
  });

  test("wraps backward past the first row to the last", () => {
    expect(moveHighlight(0, -1, 3)).toBe(2);
  });

  test("ArrowDown from unselected (-1) lands on the first row", () => {
    expect(moveHighlight(-1, 1, 3)).toBe(0);
  });

  test("ArrowUp from unselected (-1) lands on the last row", () => {
    expect(moveHighlight(-1, -1, 3)).toBe(2);
  });
});
