import { describe, expect, test } from "bun:test";
import { enterPrompt } from "@apps/explorer/listing/enter-prompt";

describe("enterPrompt", () => {
  test("names a resolved directory instead of promising a search", () => {
    expect(
      enterPrompt({ status: "exists", path: "/home/a/work/data/", is_dir: true }, "anything"),
    ).toBe("Press Enter to open data");
  });

  test("names a resolved file the same way", () => {
    expect(
      enterPrompt({ status: "exists", path: "/home/a/work/notes.md", is_dir: false }, "anything"),
    ).toBe("Press Enter to open notes.md");
  });

  test("names the folder before the first glob segment", () => {
    expect(enterPrompt({ status: "idle" }, "~/Work/*/*.json")).toBe(
      "Press Enter to open ~/Work and search",
    );
  });

  test("a glob directly under a bare tilde names just the tilde", () => {
    expect(enterPrompt({ status: "checking" }, "~/*.json")).toBe(
      "Press Enter to open ~ and search",
    );
  });

  test("an absolute glob path names its directory", () => {
    expect(enterPrompt({ status: "missing" }, "/tmp/data/*.csv")).toBe(
      "Press Enter to open /tmp/data and search",
    );
  });

  test("a relative .. prefix names the folder the search will actually walk to", () => {
    expect(enterPrompt({ status: "idle" }, "../x/*.json")).toBe(
      "Press Enter to open ../x and search",
    );
  });

  test("no glob segment: the last segment is a name pattern, dropped", () => {
    expect(enterPrompt({ status: "idle" }, "~/Work/notes")).toBe(
      "Press Enter to open ~/Work and search",
    );
  });

  test("nothing left to name after dropping falls back to generic phrasing", () => {
    expect(enterPrompt({ status: "idle" }, "~")).toBe(
      "Press Enter to open that folder and search",
    );
  });

  test("a bare root also falls back to generic phrasing", () => {
    expect(enterPrompt({ status: "idle" }, "/")).toBe(
      "Press Enter to open that folder and search",
    );
  });
});
