import { describe, expect, it } from "bun:test";
import {
  fallbackSpec,
  parseAiSearchSpec,
  rankEntries,
  scoreEntry,
} from "./ai-search";
import type { WalkEntry } from "@platform/lib/api";

const NOW = 1_800_000_000; // fixed "now" (epoch seconds) for date filters

function entry(rel: string, over: Partial<WalkEntry> = {}): WalkEntry {
  return { rel, is_dir: false, size: 1000, mtime: NOW - 3600, ...over };
}

describe("parseAiSearchSpec", () => {
  it("parses a well-formed reply", () => {
    const spec = parseAiSearchSpec(
      JSON.stringify({
        name_terms: ["weather"],
        extensions: ["CSV", ".xlsx"],
        kind: "file",
        modified_within_days: 7,
        min_size_bytes: null,
        max_size_bytes: null,
        path_hints: ["data"],
      }),
    );
    expect(spec).not.toBeNull();
    expect(spec!.extensions).toEqual(["csv", "xlsx"]); // lowercased, dot peeled
    expect(spec!.kind).toBe("file");
    expect(spec!.modified_within_days).toBe(7);
  });

  it("peels code fences the model adds despite instructions", () => {
    const spec = parseAiSearchSpec('```json\n{"name_terms": ["a"]}\n```');
    expect(spec).not.toBeNull();
    expect(spec!.name_terms).toEqual(["a"]);
    // Missing keys coerce to safe defaults, never undefined.
    expect(spec!.kind).toBe("any");
    expect(spec!.extensions).toEqual([]);
  });

  it("rejects prose, non-objects, and garbage fields", () => {
    expect(parseAiSearchSpec("Sure! Here is the JSON you asked for")).toBeNull();
    expect(parseAiSearchSpec('["not", "an", "object"]')).toBeNull();
    const spec = parseAiSearchSpec(
      JSON.stringify({
        name_terms: [1, "", "ok"],
        extensions: ["c/v", "tar.gz!", "py"],
        kind: "everything",
        modified_within_days: -5,
        min_size_bytes: "big",
      }),
    );
    expect(spec!.name_terms).toEqual(["ok"]);
    expect(spec!.extensions).toEqual(["py"]); // invalid charsets dropped
    expect(spec!.kind).toBe("any");
    expect(spec!.modified_within_days).toBeNull();
    expect(spec!.min_size_bytes).toBeNull();
  });
});

describe("scoreEntry filters", () => {
  it("enforces kind, extension, date, and size", () => {
    const spec = {
      ...fallbackSpec(""),
      extensions: ["csv"],
      kind: "file" as const,
      modified_within_days: 7,
      min_size_bytes: 500,
    };
    expect(scoreEntry(entry("data/report.csv"), spec, NOW)).not.toBeNull();
    expect(scoreEntry(entry("data/report.txt"), spec, NOW)).toBeNull();
    expect(scoreEntry(entry("data", { is_dir: true, size: null }), spec, NOW)).toBeNull();
    expect(
      scoreEntry(entry("old.csv", { mtime: NOW - 30 * 86400 }), spec, NOW),
    ).toBeNull();
    expect(scoreEntry(entry("tiny.csv", { size: 10 }), spec, NOW)).toBeNull();
  });

  it("dir hits ignore extension and size filters", () => {
    const spec = { ...fallbackSpec("data"), extensions: ["csv"], min_size_bytes: 500 };
    expect(scoreEntry(entry("data", { is_dir: true, size: null }), spec, NOW)).not.toBeNull();
  });

  it("requires at least one name term but not all (synonym lists)", () => {
    const spec = fallbackSpec("resume cv");
    expect(scoreEntry(entry("docs/resume-2024.pdf"), spec, NOW)).not.toBeNull();
    expect(scoreEntry(entry("docs/notes.txt"), spec, NOW)).toBeNull();
  });
});

describe("rankEntries", () => {
  it("orders by score then recency; term-less specs order by recency", () => {
    const spec = fallbackSpec("weather");
    const hits = rankEntries(
      [entry("misc/weather-old.csv", { mtime: NOW - 9999 }), entry("weather.csv")],
      spec,
      NOW,
    );
    // Direct name hit at a segment start beats the longer buried path.
    expect(hits[0].rel).toBe("weather.csv");

    const recent = rankEntries(
      [entry("a.txt", { mtime: NOW - 5000 }), entry("b.txt", { mtime: NOW - 10 })],
      fallbackSpec(""),
      NOW,
    );
    expect(recent[0].rel).toBe("b.txt");
  });
});
