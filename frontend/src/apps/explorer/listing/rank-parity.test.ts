// The JS half of the cross-language ranking contract.
//
// Home search now filters AND ranks on the server (fused_render/index/rank.py),
// while the in-folder search over a live streamed walk is still ranked here —
// this module is the only thing that CAN rank a stream. Two rankers answer the
// same box, so they must agree, and tests/fixtures/rank-parity.json is the
// agreement, generated from THIS ranker (`bun scripts/gen-rank-fixture.ts`).
//
// This test's job is to fail loudly when the JS ordering changes: an
// intentional change means regenerating the fixture AND updating rank.py, and
// silently letting the two drift is the bug it exists to prevent. Its mirror
// is tests/test_index_rank.py.
import { expect, test } from "bun:test";
import type { WalkEntry } from "@platform/lib/api";
import { queryWantsHidden, rankCompare, scoreEntries } from "@apps/explorer/listing/search";
import fixture from "../../../../../tests/fixtures/rank-parity.json";

const entries = fixture.entries as unknown as WalkEntry[];

test("the fixture covers the queries it says it does", () => {
  expect(fixture.queries.length).toBeGreaterThan(0);
  expect(Object.keys(fixture.expected).sort()).toEqual([...fixture.queries].sort());
});

for (const q of fixture.queries) {
  test(`the JS ranker still produces the fixture order for ${JSON.stringify(q)}`, () => {
    const hits = scoreEntries(q, entries, 0, queryWantsHidden(q));
    hits.sort(rankCompare);
    expect(hits.map((h) => h.entry.rel)).toEqual(
      (fixture.expected as Record<string, string[]>)[q],
    );
  });
}
