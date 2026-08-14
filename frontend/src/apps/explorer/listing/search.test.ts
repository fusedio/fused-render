// Ranking semantics for the in-folder search, and the cost of producing them.
//
// The comparator is the hot path: a one-character query over an index-backed
// corpus matches essentially everything, so a 200k-hit list gets sorted on
// every re-score. Anything per-COMPARISON in here is multiplied by ~3.5M.
import { expect, test } from "bun:test";
import type { WalkEntry } from "@platform/lib/api";
import { rankCompare, scoreEntries } from "@apps/explorer/listing/search";

function entry(rel: string): WalkEntry {
  return { rel, is_dir: false, size: 1, mtime: 1 } as WalkEntry;
}

function ranked(q: string, rels: string[]): string[] {
  return scoreEntries(q, rels.map(entry), 0, true)
    .sort(rankCompare)
    .map((h) => h.entry.rel);
}

test("an exact name match outranks a longer name containing it", () => {
  expect(ranked("downloads", ["a/DownloadStage.ts", "a/Downloads"])[0]).toBe("a/Downloads");
});

test("an entry whose own name matches beats one that only inherits an ancestor", () => {
  // scoreEntries matches the whole rel path, so a matching ANCESTOR DIRECTORY
  // donated its score to every descendant: "render" scored
  // render/a/b/c/d/e/f/deep-thing.bin at 26 and myrender.ts at 21, and depth is
  // only reachable on an exact score tie, so the deep file won. The name-match
  // tier sits above score and settles it.
  expect(ranked("render", ["render/a/b/c/d/junk.bin", "myrender.ts"])[0]).toBe(
    "myrender.ts",
  );
});

test("a fuzzy name match still beats an ancestor-only match", () => {
  // Tier 2 vs tier 3: neither name contains "cfg" as a substring, but one has
  // the matched characters inside its own name.
  expect(ranked("cfg", ["c/f/g/notes.txt", "mycxfxg.txt"])[0]).toBe("mycxfxg.txt");
});

test("a substring match still outranks a fuzzy one", () => {
  // Structural, and it must stay that way: fuzzyMatch's substring branch sets
  // longestRun = q.length (the maximum), which the subsequence branch can never
  // reach, so longestRun as the PRIMARY key decides this before the tier or the
  // score is looked at. Here the fuzzy hit has more than twice the score.
  expect(ranked("cfg", ["c/f/g/notes.txt", "mycfgfile.txt"])[0]).toBe(
    "mycfgfile.txt",
  );
});

test("a whole-path smear is not a hit at all", () => {
  // The span bound (lib/fuzzy `maxSpan`). `storepy` over this path is a valid
  // subsequence spread across 37 characters — s from `fused`, t from
  // `templates`, and so on — and it used to rank among the real store.py hits.
  //
  // The tier is deliberately NOT tested alongside it: tightening cannot move the
  // LAST matched position (the end is fixed by the forward pass), and
  // `nameTier`'s ancestors-only rule reads exactly that position, so tier
  // semantics are unchanged by construction.
  const smear = "fused_render/templates/autocad_viewer/tests/test_browse.py";
  expect(scoreEntries("storepy", [entry(smear)], 0, true)).toEqual([]);
  expect(ranked("storepy", [smear, "fused_render/index/store.py"])).toEqual([
    "fused_render/index/store.py",
  ]);
});

test("shallower paths win ties", () => {
  expect(ranked("readme", ["a/b/c/readme.md", "readme.md"])[0]).toBe("readme.md");
});

test("ordering is case- and accent-insensitive", () => {
  // sensitivity "base": Abc / abc / ábc collate together, and all of them
  // before "b" — NOT after "z", which is where a raw code-unit sort puts the
  // accented one. This is the property that forbids a plain `<` comparator.
  expect(ranked("x", ["zeta-x", "Ábc-x", "abc-x", "b-x"])).toEqual([
    "Ábc-x",
    "abc-x",
    "b-x",
    "zeta-x",
  ]);
});

test("the comparator does no per-comparison string splitting", () => {
  // Depth is a property of the hit, computed once when it is scored. Deriving
  // it inside the comparator meant two split("/") allocations per comparison.
  const hits = scoreEntries("c", [entry("a/b/c.ts"), entry("c.ts")], 0, true);
  expect(hits.map((h) => h.depth)).toEqual([3, 1]);
});

test("ranking a large hit set stays well under a frame budget", () => {
  // The regression this guards: rankCompare called String#localeCompare with
  // an options bag, which constructs a fresh ICU collator on EVERY call —
  // 3.8s to sort 200k hits, i.e. the typing freeze. A hoisted collator is the
  // same collation at ~2% of the cost. Threshold is deliberately loose (CI is
  // slower than a laptop); the bug was 40x over it.
  const entries: WalkEntry[] = [];
  for (let i = 0; i < 150_000; i++) entries.push(entry(`src/m${i % 300}/c${i}.tsx`));
  const hits = scoreEntries("c", entries, 0, false);
  expect(hits.length).toBe(150_000);
  const t0 = performance.now();
  hits.sort(rankCompare);
  expect(performance.now() - t0).toBeLessThan(1000);
});
