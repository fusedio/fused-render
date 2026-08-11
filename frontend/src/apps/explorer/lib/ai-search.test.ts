import { afterEach, describe, expect, it, mock } from "bun:test";
import {
  hasNonNameFilters,
  parseAiSearchSpec,
  rankHits,
  runAiSearch,
  type AiSearchSpec,
} from "./ai-search";
import type { SearchFileEntry } from "@platform/lib/api";

const HOME = "/Users/me";
const NOW = 1_800_000_000; // epoch seconds, for mtime ordering only

function entry(rel: string, over: Partial<SearchFileEntry> = {}): SearchFileEntry {
  return { path: `${HOME}/${rel}`, is_dir: false, size: 1000, mtime: NOW - 3600, ...over };
}

// A spec builder for the tests. There is no `fallbackSpec` any more — an
// unusable model reply is reported, not substituted (see runAiSearch) — so the
// empty-spec shape lives here, where only the tests need it.
function spec(over: Partial<AiSearchSpec> = {}): AiSearchSpec {
  return {
    name_terms: [],
    extensions: [],
    kind: "any",
    modified_after: null,
    modified_before: null,
    min_size_bytes: null,
    max_size_bytes: null,
    path_hints: [],
    ...over,
  };
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
    expect(hasNonNameFilters(spec({ name_terms: ["weather"] }))).toBe(false);
    expect(hasNonNameFilters(spec({ extensions: ["mov"] }))).toBe(true);
    expect(hasNonNameFilters(spec({ modified_after: "2026-08-04" }))).toBe(true);
    expect(hasNonNameFilters(spec({ max_size_bytes: 100 }))).toBe(true);
  });
});

describe("rankHits", () => {
  it("hard-drops non-matching entries when name terms are the only filter", () => {
    const hits = rankHits(
      [entry("docs/resume-2024.pdf"), entry("docs/notes.txt")],
      spec({ name_terms: ["resume", "cv"] }),
      HOME,
    );
    expect(hits.map((h) => h.path)).toEqual([`${HOME}/docs/resume-2024.pdf`]);
  });

  it("keeps unmatched entries when other filters exist (soft terms)", () => {
    // "video downloaded today": extension+date pinned, "video" not in the
    // filename — IMG_1234.mov must survive and matching names rank first.
    const hits = rankHits(
      [entry("Downloads/IMG_1234.mov"), entry("Movies/video-final.mp4")],
      spec({
        name_terms: ["video"],
        extensions: ["mov", "mp4"],
        modified_after: "2026-08-04",
      }),
      HOME,
    );
    expect(hits).toHaveLength(2);
    expect(hits[0].path).toBe(`${HOME}/Movies/video-final.mp4`);
  });

  it("boosts path hints and orders term-less specs by recency", () => {
    const withHints = spec({ extensions: ["mov"], path_hints: ["downloads"] });
    const hits = rankHits(
      [
        entry("Movies/a.mov", { mtime: NOW - 10 }),
        entry("Downloads/b.mov", { mtime: NOW - 9999 }),
      ],
      withHints,
      HOME,
    );
    // The hint boost outweighs recency; without hints recency would win.
    expect(hits[0].path).toBe(`${HOME}/Downloads/b.mov`);

    const noHints = rankHits(
      [
        entry("Movies/a.mov", { mtime: NOW - 10 }),
        entry("Downloads/b.mov", { mtime: NOW - 9999 }),
      ],
      { ...withHints, path_hints: [] },
      HOME,
    );
    expect(noHints[0].path).toBe(`${HOME}/Movies/a.mov`);
  });

  it("scores against the home-relative path, not /Users/<name>", () => {
    // "me" appears in /Users/me — rooting at "/" would match every entry.
    const hits = rankHits(
      [entry("Downloads/movie.mov"), entry("notes.txt")],
      spec({ name_terms: ["me"] }),
      HOME,
    );
    expect(hits.map((h) => h.path)).toEqual([`${HOME}/Downloads/movie.mov`]);
  });
});

// -- runAiSearch: one model call, one engine query, no substitutions ----------

const realFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = realFetch;
});

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** Stub /api/ai and /api/search/files; records every request URL in order. */
function stubApi(opts: {
  aiText?: string;
  aiStatus?: number;
  entries?: SearchFileEntry[];
}): string[] {
  const calls: string[] = [];
  globalThis.fetch = mock(async (url: string | URL | Request) => {
    const u = String(url);
    calls.push(u);
    if (u === "/api/ai") {
      if (opts.aiStatus && opts.aiStatus !== 200)
        return jsonResponse({ error: "claude is not installed" }, opts.aiStatus);
      return jsonResponse({ ok: true, result: { text: opts.aiText ?? "{}" } });
    }
    if (u === "/api/search/files")
      return jsonResponse({ ok: true, entries: opts.entries ?? [], truncated: false });
    throw new Error("unexpected fetch: " + u);
  }) as unknown as typeof fetch;
  return calls;
}

describe("runAiSearch", () => {
  it("makes exactly one model call and one engine query", async () => {
    const calls = stubApi({
      aiText: JSON.stringify({ name_terms: ["weather"], extensions: ["csv"] }),
      entries: [entry("data/weather.csv")],
    });
    const res = await runAiSearch(HOME, "weather spreadsheet");
    expect(calls).toEqual(["/api/ai", "/api/search/files"]);
    expect(res.hits.map((h) => h.path)).toEqual([`${HOME}/data/weather.csv`]);
    expect(res.spec.name_terms).toEqual(["weather"]);
  });

  it("does not retry the engine when the first result set is empty", async () => {
    // The old pipeline fired a second /api/search/files with name_terms
    // stripped. AI search is now a deliberate action over instant index
    // results, so an empty answer is the answer.
    const calls = stubApi({
      aiText: JSON.stringify({ name_terms: ["weather"], extensions: ["csv"] }),
      entries: [],
    });
    const res = await runAiSearch(HOME, "weather spreadsheet");
    expect(calls.filter((c) => c === "/api/search/files")).toHaveLength(1);
    expect(res.hits).toEqual([]);
  });

  it("reports an unusable model reply instead of searching the raw words", async () => {
    const calls = stubApi({ aiText: "Sure! I can help you find files." });
    await expect(runAiSearch(HOME, "weather")).rejects.toThrow(/could not/i);
    // Nothing was sent to the engine: a keyword search on the user's own words
    // would be a different question answered silently.
    expect(calls).toEqual(["/api/ai"]);
  });

  it("reports a spec that narrows nothing the engine understands", async () => {
    // "in downloads" parses fine but only sets path_hints, which is
    // client-side ranking and never reaches the engine.
    const calls = stubApi({ aiText: JSON.stringify({ path_hints: ["downloads"] }) });
    await expect(runAiSearch(HOME, "stuff in downloads")).rejects.toThrow(/narrow/i);
    expect(calls).toEqual(["/api/ai"]);
  });

  it("propagates a relay failure", async () => {
    stubApi({ aiStatus: 503 });
    await expect(runAiSearch(HOME, "weather")).rejects.toThrow(/claude is not installed/);
  });
});
