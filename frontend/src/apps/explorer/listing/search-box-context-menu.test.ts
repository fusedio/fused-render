import { describe, expect, test } from "bun:test";
import { searchBoxRestingForContextMenu } from "@apps/explorer/listing/search-box-context-menu";

describe("searchBoxRestingForContextMenu", () => {
  test("resting (empty query, not pinned open) opens the bar menu", () => {
    expect(searchBoxRestingForContextMenu("", false)).toBe(true);
  });

  test("stands down once a query is typed, even if the box is not pinned open", () => {
    expect(searchBoxRestingForContextMenu("*.csv", false)).toBe(false);
  });

  test("stands down while focused-and-empty (pinnedOpen, no query yet)", () => {
    expect(searchBoxRestingForContextMenu("", true)).toBe(false);
  });

  test("stands down with both a query typed and the box pinned open", () => {
    expect(searchBoxRestingForContextMenu("~/Work", true)).toBe(false);
  });
});
