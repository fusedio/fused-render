import { afterEach, describe, expect, test } from "bun:test";
import {
  __setSupportsIndicesFlagForTest,
  fuzzyMatch,
  globMatch,
  highlightSegments,
  maxSpan,
} from "./fuzzy";

// The reported case: greedy-earliest alignment bound `i` to `iamsdas`, `n` to
// `render` and so on, smearing an 8-char query across the whole path while a
// near-perfect match sat in the last segment.
const REL = "fused_render/index/specs/index-store.md";
const ABS = "/Users/iamsdas/Work/fused-render/" + REL;

describe("tightening", () => {
  test("the match snaps onto the tail segment, not the earliest letters", () => {
    const m = fuzzyMatch("index.md", REL)!;
    expect(m).not.toBeNull();
    // `index` of index-store.md, then `.md` — not one letter each from
    // fused_render / index / specs.
    expect(m.positions).toEqual([25, 26, 27, 28, 29, 36, 37, 38]);
    expect(REL.slice(25, 30)).toBe("index");
  });

  test("score and longestRun are recomputed from the tightened alignment", () => {
    // Greedy binds `a` to index 0 and gets a run of 2 (`bc`); packing leftwards
    // from the same end finds the `abc` at index 2 and a run of 3. Judging the
    // greedy numbers would have under-scored a better match.
    const m = fuzzyMatch("abcd", "aXabcYd")!;
    expect(m.positions).toEqual([2, 3, 4, 6]);
    expect(m.longestRun).toBe(3);
    expect(fuzzyMatch("index.md", REL)!.longestRun).toBe(5);
  });

  test("the highlight no longer scatters across the leading path", () => {
    const m = fuzzyMatch("index.md", ABS)!;
    const marked = highlightSegments(ABS, m.positions)
      .filter((s) => s.match)
      .map((s) => s.text);
    expect(marked).toEqual(["index", ".md"]);
    // Every marked char is inside the final segment; none is in the leading
    // /Users/iamsdas/Work/… that used to donate letters.
    const lastSlash = ABS.lastIndexOf("/");
    expect(Math.min(...m.positions)).toBeGreaterThan(lastSlash);
  });

  test("tightening cannot lose a match the greedy pass found", () => {
    // The forward pass proves feasibility and fixes the end; the backward pass
    // is then always satisfiable from it.
    for (const [q, t] of [
      ["abc", "a-b-c"],
      ["abc", "aabbcc"],
      ["aaa", "aaaa"],
      ["ab", "ba-ab"],
    ] as const) {
      const m = fuzzyMatch(q, t);
      expect(m, `${q} in ${t}`).not.toBeNull();
      expect(m!.positions.length).toBe(q.length);
    }
  });

  test("positions stay ascending and one per query char", () => {
    const m = fuzzyMatch("aaa", "aXaYaZa")!;
    expect(m.positions.length).toBe(3);
    for (let i = 1; i < m.positions.length; i++) {
      expect(m.positions[i]).toBeGreaterThan(m.positions[i - 1]);
    }
  });

  test("the last query char binds as late as the earliest end allows", () => {
    // "ab" over "a-b-b": the end is fixed at the FIRST reachable b, so the
    // second b is not chased — tightening packs leftwards from a fixed end, it
    // does not hunt for the tail of the string.
    //
    // Worth stating because a consumer depends on it: listing/search.ts's
    // `nameTier` grades a hit ancestors-only from the LAST position, and that
    // position is the one thing tightening never moves. Tier semantics are
    // therefore unchanged by construction, not by luck.
    expect(fuzzyMatch("ab", "a-b-b")!.positions).toEqual([0, 2]);
  });
});

describe("the span bound", () => {
  test("it grows with the query, so a long query may legitimately spread", () => {
    expect(maxSpan(2)).toBe(14);
    expect(maxSpan(8)).toBe(32);
    expect(maxSpan(14)).toBe(50);
  });

  test("a match exactly at the bound is kept and one char wider is not", () => {
    const at = "a" + "z".repeat(maxSpan(2) - 2) + "b";
    const over = "a" + "z".repeat(maxSpan(2) - 1) + "b";
    expect(fuzzyMatch("ab", at)).not.toBeNull();
    expect(fuzzyMatch("ab", over)).toBeNull();
  });

  test("the reported scatter is refused outright", () => {
    // Real repo paths where the ONLY alignment for `index.md` is a whole-path
    // smear: nothing named index, nothing ending index-ish.
    for (const t of [
      "/Users/iamsdas/Work/fused-render/docs/EXPORT.md",
      "/Users/iamsdas/Work/fused-render/docs/LINUX_DESKTOP_SPEC.md",
    ]) {
      expect(fuzzyMatch("index.md", t), t).toBeNull();
    }
  });

  test("multi-segment queries a user really types keep matching", () => {
    // Each of these spans several path segments and is exactly the intent the
    // matcher exists for; a per-gap cap tight enough to catch the scatter above
    // would have killed them.
    for (const [q, t] of [
      ["index.md", REL],
      ["explorersearch", "frontend/src/apps/explorer/listing/useListingSearch.ts"],
      ["fusedindex", "fused_render/index/freshness.py"],
      ["indexstore", "fused_render/index/specs/index-store.md"],
      ["storepy", "fused_render/shell/mounts/store.py"],
      ["srcstyles", "frontend/src/styles/account.css"],
      ["fris", "frontend/src/platform/ui/Skeleton.tsx"],
      ["specsscanmd", "fused_render/index/specs/scan-incremental.md"],
    ] as const) {
      expect(fuzzyMatch(q, t), `${q} in ${t}`).not.toBeNull();
    }
  });

  test("the named cost: a 3-char initialism over a long prose title is refused", () => {
    // Not a bug — a measured trade, recorded so it is not rediscovered as one.
    // The bookmark search matches against page titles, and word-initials over a
    // long one spread further than a short query's bound allows. The obvious fix
    // (an allowance per segment-start hit) re-admits the whole-path smear this
    // bound exists for — see maxSpan's comment for the numbers. One more typed
    // character brings it back.
    const title = "Zarr v3 multiscale pyramid budget notes";
    expect(fuzzyMatch("zmp", title)).toBeNull();
    // Typing more of the first word buys the span back.
    expect(fuzzyMatch("zarrmp", title)).not.toBeNull();
  });

  test("the bound is on the whole span, not on each gap", () => {
    // Two tight halves separated by one long gap is a GOOD match ("index" then
    // ".md" across a directory name), so a per-gap cap has to be loose — and
    // once it is loose enough for that it no longer catches the scatter.
    expect(fuzzyMatch("indexmd", "index/a-fairly-long-folder-name/x.md")).not
      .toBeNull();
  });
});

describe("the substring fast path is untouched", () => {
  test("a substring sets longestRun to the query length", () => {
    // rankCompare orders on longestRun FIRST, and this is the invariant that
    // guarantees substring-over-fuzzy (listing/search.ts).
    const m = fuzzyMatch("index", REL)!;
    expect(m.longestRun).toBe(5);
    expect(m.positions).toEqual([13, 14, 15, 16, 17]);
  });

  test("a substring is never refused for its span", () => {
    // Spans are irrelevant here — a substring's span IS the query length — but
    // the branch must also stay ahead of the bound check.
    const long = "a".repeat(200) + "needle";
    expect(fuzzyMatch("needle", long)!.longestRun).toBe(6);
  });

  test("a whole-text match still works", () => {
    expect(fuzzyMatch("abc", "abc")!.positions).toEqual([0, 1, 2]);
  });
});

describe("glob highlighting (SPEC-search-space-wildcard.md §4)", () => {
  test("marks each literal piece separately, wildcard gaps unmarked", () => {
    const positions = globMatch("**/*hello*world*", "my_hello_big_world.py")!.positions;
    const segs = highlightSegments("my_hello_big_world.py", positions)
      .filter((s) => s.match)
      .map((s) => s.text);
    expect(segs).toEqual(["hello", "world"]);
  });

  test("an explicitly typed glob highlights its literal pieces the same way", () => {
    const positions = globMatch("**/*.pdf", "Q3 report.pdf")!.positions;
    const segs = highlightSegments("Q3 report.pdf", positions)
      .filter((s) => s.match)
      .map((s) => s.text);
    expect(segs).toEqual([".pdf"]);
  });

  test("a directory-crossing glob marks literal pieces on both sides of **", () => {
    const positions = globMatch("src/**/*.ts", "src/lib/util.ts")!.positions;
    const segs = highlightSegments("src/lib/util.ts", positions)
      .filter((s) => s.match)
      .map((s) => s.text);
    expect(segs).toEqual(["src", ".ts"]);
  });

  test("null when the pattern does not actually match the text", () => {
    expect(globMatch("**/*.pdf", "report.csv")).toBeNull();
  });

  test("case-insensitive, like every other matcher here", () => {
    const positions = globMatch("**/*hello*", "HELLO.txt")!.positions;
    const segs = highlightSegments("HELLO.txt", positions)
      .filter((s) => s.match)
      .map((s) => s.text);
    expect(segs).toEqual(["HELLO"]);
  });

  test("score and longestRun are 0 — glob mode has no scoring", () => {
    const m = globMatch("**/*.pdf", "report.pdf")!;
    expect(m.score).toBe(0);
    expect(m.longestRun).toBe(0);
  });
});

describe("globMatch without the regex 'd' flag (code review finding: WebKit < 16.4)", () => {
  // The "d" flag (`hasIndices`) is unsupported on WebKit < 16.4 — not a
  // feature `exec` degrades gracefully on, but one `new RegExp` itself
  // THROWS a `SyntaxError` for, at construction time. Forcing the cached
  // probe to `false` (rather than actually breaking `new RegExp` globally)
  // exercises `globMatch`'s fallback branch exactly as an affected browser
  // would hit it, on every real engine this suite runs against.
  afterEach(() => {
    __setSupportsIndicesFlagForTest(undefined);
  });

  test("does not throw, and still finds each literal piece", () => {
    __setSupportsIndicesFlagForTest(false);
    expect(() => globMatch("**/*hello*world*", "my_hello_big_world.py")).not.toThrow();
    const positions = globMatch("**/*hello*world*", "my_hello_big_world.py")!.positions;
    const segs = highlightSegments("my_hello_big_world.py", positions)
      .filter((s) => s.match)
      .map((s) => s.text);
    expect(segs).toEqual(["hello", "world"]);
  });

  test("a directory-crossing glob still marks both sides without indices", () => {
    __setSupportsIndicesFlagForTest(false);
    const positions = globMatch("src/**/*.ts", "src/lib/util.ts")!.positions;
    const segs = highlightSegments("src/lib/util.ts", positions)
      .filter((s) => s.match)
      .map((s) => s.text);
    expect(segs).toEqual(["src", ".ts"]);
  });

  test("still null when the pattern does not actually match the text", () => {
    __setSupportsIndicesFlagForTest(false);
    expect(globMatch("**/*.pdf", "report.csv")).toBeNull();
  });

  test("still case-insensitive without the flag", () => {
    __setSupportsIndicesFlagForTest(false);
    const positions = globMatch("**/*hello*", "HELLO.txt")!.positions;
    const segs = highlightSegments("HELLO.txt", positions)
      .filter((s) => s.match)
      .map((s) => s.text);
    expect(segs).toEqual(["HELLO"]);
  });
});

describe("unchanged contracts", () => {
  test("an empty query is a zero match, not a miss", () => {
    expect(fuzzyMatch("", "anything")).toEqual({
      score: 0,
      positions: [],
      longestRun: 0,
    });
  });

  test("a char that is not there at all is still null", () => {
    expect(fuzzyMatch("xyz", "abc")).toBeNull();
    expect(fuzzyMatch("abcd", "abc")).toBeNull();
  });

  test("matching is case-insensitive and highlights the original case", () => {
    // And the tighten shows up here too: greedy took the leading `D`, leaving
    // two islands; packing leftwards lands on `dM`, the camel-hump seam a human
    // typing "dm" meant.
    const m = fuzzyMatch("dm", "DownloadManager.tsx")!;
    expect(m.positions).toEqual([7, 8]);
    expect(highlightSegments("DownloadManager.tsx", m.positions)
      .filter((s) => s.match)
      .map((s) => s.text)).toEqual(["dM"]);
  });
});
