import { describe, expect, test } from "bun:test";
import { searchBoxBlurAction } from "@apps/explorer/listing/search-provisional";

describe("searchBoxBlurAction", () => {
  test("an uncommitted query is discarded on blur, even though it isn't empty", () => {
    expect(searchBoxBlurAction(false, false)).toBe("discard");
  });

  test("a committed query survives a blur", () => {
    expect(searchBoxBlurAction(true, false)).toBe("keep-open");
  });

  test("an auto-searching query, once its results are on screen, survives a blur", () => {
    // Same case as the row above, named for the query shape that never needs
    // Enter to get there: `*.zip` reaches `committed: true` on its own.
    expect(searchBoxBlurAction(true, false)).toBe("keep-open");
  });

  test("an emptied, committed field unpins on blur", () => {
    expect(searchBoxBlurAction(true, true)).toBe("unpin");
  });

  test("an emptied, uncommitted field unpins on blur", () => {
    expect(searchBoxBlurAction(false, true)).toBe("unpin");
  });
});
