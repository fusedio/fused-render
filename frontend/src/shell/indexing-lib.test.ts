// The pure half of Indexing.tsx: text<->pattern conversion, stale-default
// detection, the "Restore defaults" union merge, and the one line of a failed
// scan's traceback the panel shows.
import { describe, expect, it } from "bun:test";
import {
  missingDefaults,
  patternsToText,
  scanErrorLine,
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
    expect(unionWithDefaults("", ["node_modules", "dist"])).toBe("node_modules\ndist");
  });

  it("drops a trailing blank line from a newline-terminated list before appending", () => {
    const text = "a\n";
    expect(unionWithDefaults(text, ["node_modules"])).toBe("a\nnode_modules");
  });

  it("keeps an interior blank line the user typed", () => {
    const text = "a\n\nb";
    expect(unionWithDefaults(text, ["node_modules"])).toBe("a\n\nb\nnode_modules");
  });
});

describe("scanErrorLine", () => {
  it("pulls the exception out of a Python traceback", () => {
    // What a failed scan actually reports (traceback.format_exc()). The
    // actionable half is the last line; the rest is this app's own files.
    const tb = [
      "Traceback (most recent call last):",
      '  File "/opt/fused/index/scan.py", line 214, in _walk',
      "    store.flush(rows)",
      "OSError: [Errno 28] No space left on device",
    ].join("\n");
    expect(scanErrorLine(tb)).toBe("OSError: [Errno 28] No space left on device");
  });

  it("passes a one-line error through unchanged", () => {
    // An abandoned worker's own message is already the whole story.
    expect(scanErrorLine("worker exited without finishing")).toBe(
      "worker exited without finishing",
    );
  });

  it("survives trailing newlines and an all-blank value", () => {
    expect(scanErrorLine("ValueError: bad root\n\n")).toBe("ValueError: bad root");
    expect(scanErrorLine("\n  \n")).toBe("");
  });
});
