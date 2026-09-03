// The pure half of Indexing.tsx: text<->pattern conversion, stale-default
// detection, and the "Restore defaults" union merge.
import { describe, expect, it } from "bun:test";
import {
  missingDefaults,
  patternsToText,
  textToPatterns,
  unionWithDefaults,
} from "./indexing-lib";

describe("patternsToText / textToPatterns", () => {
  it("round-trips a pattern list through newline-joined text", () => {
    const patterns = ["node_modules", "*.egg-info", "~/Library/Caches"];
    expect(textToPatterns(patternsToText(patterns))).toEqual(patterns);
  });
});

describe("missingDefaults", () => {
  it("is empty when the saved list already has every default", () => {
    expect(missingDefaults(["node_modules", "dist"], ["node_modules", "dist"])).toEqual([]);
  });

  it("names defaults absent from the saved list", () => {
    expect(missingDefaults(["node_modules"], ["node_modules", "dist", "build"])).toEqual([
      "dist",
      "build",
    ]);
  });

  it("is order-insensitive: a reordered saved list is not stale", () => {
    expect(missingDefaults(["dist", "node_modules"], ["node_modules", "dist"])).toEqual([]);
  });

  it("a user's own extra entries do not count as missing, and do not hide real gaps", () => {
    expect(
      missingDefaults(["node_modules", "my-secret-cache"], ["node_modules", "dist"])
    ).toEqual(["dist"]);
  });

  it("ignores comments and blank lines when checking what is present", () => {
    expect(
      missingDefaults(["# my rules", "node_modules", "", "dist"], ["node_modules", "dist"])
    ).toEqual([]);
  });

  it("everything is missing from an empty saved list", () => {
    expect(missingDefaults([], ["node_modules", "dist"])).toEqual(["node_modules", "dist"]);
  });
});

describe("unionWithDefaults", () => {
  it("is a no-op when nothing is missing", () => {
    const text = "node_modules\ndist";
    expect(unionWithDefaults(text, ["node_modules", "dist"])).toBe(text);
  });

  it("appends missing defaults after the user's existing lines", () => {
    const text = "node_modules\nmy-cache";
    expect(unionWithDefaults(text, ["node_modules", "dist", "build"])).toBe(
      "node_modules\nmy-cache\ndist\nbuild"
    );
  });

  it("preserves comments and blank lines and ordering of existing lines", () => {
    const text = "# my rules\nnode_modules\n\nmy-cache";
    expect(unionWithDefaults(text, ["node_modules", "dist"])).toBe(
      "# my rules\nnode_modules\n\nmy-cache\ndist"
    );
  });

  it("builds the full default list when starting from empty text", () => {
    expect(unionWithDefaults("", ["node_modules", "dist"])).toBe("\nnode_modules\ndist");
  });
});
