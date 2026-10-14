import { describe, expect, test } from "bun:test";
import { readmeLead, trimReadme } from "./OverviewTabs";

describe("trimReadme", () => {
  test("drops the leading title and the preview line, keeps the prose", () => {
    const md = "# Sine\n\n![shot](preview.png)\n\nThe smallest app.\n";
    expect(trimReadme(md)).toBe("The smallest app.\n");
  });
  test("leaves a readme that does not open with them alone", () => {
    expect(trimReadme("Intro\n\n# Later")).toBe("Intro\n\n# Later");
  });
});

describe("readmeLead", () => {
  test("is the first prose paragraph as plain text", () => {
    const md = "# Demo\n\n![x](preview.png)\n\nA **demo** app with [docs](https://x.y).\n\n- a list";
    expect(readmeLead(md)).toBe("A demo app with docs.");
  });
  test("skips lists and code to the first paragraph", () => {
    expect(readmeLead("- one\n- two\n\n```\ncode\n```\n\nThen prose.")).toBe("Then prose.");
  });
  test("clips long paragraphs on a word with an ellipsis", () => {
    const lead = readmeLead("word ".repeat(80), 40) ?? "";
    expect(lead.endsWith("…")).toBe(true);
    expect(lead.length).toBeLessThanOrEqual(41);
  });
  test("null when there is no paragraph", () => {
    expect(readmeLead("# Only a title")).toBeNull();
  });
});
