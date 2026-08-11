import { describe, expect, test } from "bun:test";
import { fuzzyMatch, highlightSegments, maxSpan } from "./fuzzy";

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
      ["explorersearch", "frontend/src/apps/explorer/listing/useWalkSearch.ts"],
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
