import { describe, expect, it } from "bun:test";
import {
  fallbackSpec,
  hasEngineNarrowing,
  hasNonNameFilters,
  parseAiSearchSpec,
  rankHits,
} from "./ai-search";
import type { SearchFileEntry } from "@platform/lib/api";

const HOME = "/Users/me";
const NOW = 1_800_000_000; // epoch seconds, for mtime ordering only

function entry(rel: string, over: Partial<SearchFileEntry> = {}): SearchFileEntry {
  return { path: `${HOME}/${rel}`, is_dir: false, size: 1000, mtime: NOW - 3600, ...over };
}

describe("parseAiSearchSpec", () => {
  it("parses a well-formed reply", () => {
    const spec = parseAiSearchSpec(
      JSON.stringify({
        name_terms: ["weather"],
        extensions: ["CSV", ".xlsx"],
        kind: "file",
        modified_after: "2026-08-01",
        modified_before: "2026-08-05",
        min_size_bytes: null,
        max_size_bytes: null,
        path_hints: ["data"],
      }),
    );
    expect(spec).not.toBeNull();
    expect(spec!.extensions).toEqual(["csv", "xlsx"]); // lowercased, dot peeled
    expect(spec!.kind).toBe("file");
    expect(spec!.modified_after).toBe("2026-08-01");
    expect(spec!.modified_before).toBe("2026-08-05");
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
        modified_after: "last week", // not a date
        modified_before: "2026-02-31", // shape of a date, not a real one
        min_size_bytes: "big",
      }),
    );
    expect(spec!.name_terms).toEqual(["ok"]);
    expect(spec!.extensions).toEqual(["py"]); // invalid charsets dropped
    expect(spec!.kind).toBe("any");
    expect(spec!.modified_after).toBeNull();
    expect(spec!.modified_before).toBeNull();
    expect(spec!.min_size_bytes).toBeNull();
  });
});

describe("hasNonNameFilters", () => {
  it("is false for a name-only spec, true once any real filter is set", () => {
    expect(hasNonNameFilters(fallbackSpec("weather"))).toBe(false);
    expect(hasNonNameFilters({ ...fallbackSpec(""), extensions: ["mov"] })).toBe(true);
    expect(hasNonNameFilters({ ...fallbackSpec(""), modified_after: "2026-08-04" })).toBe(true);
    expect(hasNonNameFilters({ ...fallbackSpec(""), max_size_bytes: 100 })).toBe(true);
  });
});

describe("hasEngineNarrowing", () => {
  it("is false for a location/kind-only spec — path_hints never reach the engine", () => {
    expect(hasEngineNarrowing({ ...fallbackSpec(""), path_hints: ["downloads"] })).toBe(false);
    expect(hasEngineNarrowing({ ...fallbackSpec(""), kind: "dir" })).toBe(false);
  });

  it("is true once name terms or any real filter is set", () => {
    expect(hasEngineNarrowing(fallbackSpec("weather"))).toBe(true);
    expect(hasEngineNarrowing({ ...fallbackSpec(""), extensions: ["mov"] })).toBe(true);
  });
});

describe("rankHits", () => {
  it("hard-drops non-matching entries when name terms are the only filter", () => {
    const spec = fallbackSpec("resume cv");
    const hits = rankHits(
      [entry("docs/resume-2024.pdf"), entry("docs/notes.txt")],
      spec,
      HOME,
    );
    expect(hits.map((h) => h.path)).toEqual([`${HOME}/docs/resume-2024.pdf`]);
  });

  it("keeps unmatched entries when other filters exist (soft terms)", () => {
    // "video downloaded today": extension+date pinned, "video" not in the
    // filename — IMG_1234.mov must survive and matching names rank first.
    const spec = {
      ...fallbackSpec("video"),
      extensions: ["mov", "mp4"],
      modified_after: "2026-08-04",
    };
    const hits = rankHits(
      [entry("Downloads/IMG_1234.mov"), entry("Movies/video-final.mp4")],
      spec,
      HOME,
    );
    expect(hits).toHaveLength(2);
    expect(hits[0].path).toBe(`${HOME}/Movies/video-final.mp4`);
  });

  it("boosts path hints and orders term-less specs by recency", () => {
    const spec = { ...fallbackSpec(""), extensions: ["mov"], path_hints: ["downloads"] };
    const hits = rankHits(
      [
        entry("Movies/a.mov", { mtime: NOW - 10 }),
        entry("Downloads/b.mov", { mtime: NOW - 9999 }),
      ],
      spec,
      HOME,
    );
    // The hint boost outweighs recency; without hints recency would win.
    expect(hits[0].path).toBe(`${HOME}/Downloads/b.mov`);

    const noHints = rankHits(
      [
        entry("Movies/a.mov", { mtime: NOW - 10 }),
        entry("Downloads/b.mov", { mtime: NOW - 9999 }),
      ],
      { ...spec, path_hints: [] },
      HOME,
    );
    expect(noHints[0].path).toBe(`${HOME}/Movies/a.mov`);
  });

  it("scores against the home-relative path, not /Users/<name>", () => {
    // "me" appears in /Users/me — rooting at "/" would match every entry.
    const spec = fallbackSpec("me");
    const hits = rankHits([entry("Downloads/movie.mov"), entry("notes.txt")], spec, HOME);
    expect(hits.map((h) => h.path)).toEqual([`${HOME}/Downloads/movie.mov`]);
  });
});
