import { describe, expect, test } from "bun:test";
import { enterPrompt } from "@apps/explorer/listing/enter-prompt";

describe("enterPrompt", () => {
  test("names a resolved directory instead of promising a search", () => {
    expect(
      enterPrompt({ status: "exists", path: "/home/a/work/data/", is_dir: true }),
    ).toBe("Press Enter to open data");
  });

  test("names a resolved file the same way", () => {
    expect(
      enterPrompt({ status: "exists", path: "/home/a/work/notes.md", is_dir: false }),
    ).toBe("Press Enter to open notes.md");
  });

  test("keeps the search wording while still resolving, not a third state", () => {
    expect(enterPrompt({ status: "checking" })).toBe("Press Enter to search");
  });

  test("keeps the search wording once resolution says the path is not real", () => {
    expect(enterPrompt({ status: "missing" })).toBe("Press Enter to search");
  });

  test("keeps the search wording before anything has been asked", () => {
    expect(enterPrompt({ status: "idle" })).toBe("Press Enter to search");
  });
});
