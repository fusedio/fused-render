// The ranked search box, DRIVEN: a query is typed, a reply lands, the clock
// moves past the poll interval, the folder changes under an outstanding
// request. Everything here is a sequence, which is precisely what the source
// guards next door could not test.
import { afterEach, beforeEach, describe, expect, mock, test } from "bun:test";
import type { IndexRankResult } from "@platform/lib/api";
import { Clock, Deferred, flush, renderHook } from "@apps/explorer/listing/hook-harness";

// --- the module boundary ------------------------------------------------------
const rankCalls: { root: string; q: string; reply: Deferred<IndexRankResult> }[] = [];
const scanCalls: string[] = [];
let scanReply: { started: boolean; why: string } = { started: true, why: "started" };
const walkCalls: string[] = [];

mock.module("@platform/lib/api", () => ({
  indexRank: (root: string, q: string) => {
    const reply = new Deferred<IndexRankResult>();
    rankCalls.push({ root, q, reply });
    return reply.promise;
  },
  requestFolderScan: (path: string) => {
    scanCalls.push(path);
    return Promise.resolve({ ...scanReply, run_id: "r1", root: path });
  },
  walkDirStream: (path: string) => {
    walkCalls.push(path);
    return new Promise(() => {}); // a walk nobody resolves; its start is the fact
  },
}));

mock.module("@platform/lib/router", () => ({ replaceSearch: () => {} }));

const { useWalkSearch } = await import("@apps/explorer/listing/useWalkSearch");
const freshness = await import("@platform/lib/index-freshness");

function answer(over: Partial<IndexRankResult> = {}): IndexRankResult {
  return {
    covered: true,
    fresh: true,
    reason: "",
    root: "/d",
    hits: [],
    truncated: false,
    total: 0,
    updated: 1,
    age_s: 1,
    ...over,
  };
}

const hit = (rel: string) => ({
  rel, is_dir: false, size: 1, mtime: 1, score: 10, longest_run: 3, tier: 1, depth: 1,
});

const clock = new Clock();

beforeEach(() => {
  rankCalls.length = 0;
  scanCalls.length = 0;
  walkCalls.length = 0;
  scanReply = { started: true, why: "started" };
  freshness.resetFsMutations();
  clock.install();
});
afterEach(() => clock.restore());

/** Mount the hook and type `q` into it. */
async function search(q: string, fsPath = "/d") {
  const box = renderHook((p: string, r: number) => useWalkSearch(p, r, false), fsPath, 0);
  await flush(() => box.current().setQuery(q));
  return box;
}

const SCAN_POLL_MS = 1_500;
const MAX_SCANNING_POLLS = 80;

describe("an uncovered folder: scan, poll, rows", () => {
  test("asks for a scan once, keeps saying an answer is coming, then answers", async () => {
    const box = await search("widget");
    expect(rankCalls).toHaveLength(1);

    // 1. uncovered -> one scan asked for, and the box must NOT read as a
    //    finished zero-hit result while that scan runs.
    await flush(() => rankCalls[0].reply.resolve(
      answer({ covered: false, reason: "uncovered" })));
    expect(scanCalls).toEqual(["/d"]);
    expect(box.current().scanPending).toBe(true);
    expect(box.current().displayHits).toEqual([]);

    // 2. the poll re-asks on its own cadence
    await flush(() => clock.advance(SCAN_POLL_MS));
    expect(rankCalls).toHaveLength(2);
    await flush(() => rankCalls[1].reply.resolve(
      answer({ covered: true, reason: "scanning" })));
    expect(box.current().scanPending).toBe(true);
    expect(scanCalls).toHaveLength(1); // asked ONCE, however many polls

    // 3. rows land and the polling stops
    await flush(() => clock.advance(SCAN_POLL_MS));
    await flush(() => rankCalls[2].reply.resolve(
      answer({ hits: [hit("a/widget.md")], total: 1 })));
    expect(box.current().displayHits.map((h) => h.entry.rel)).toEqual(["a/widget.md"]);
    expect(box.current().scanPending).toBe(false);

    await flush(() => clock.advance(SCAN_POLL_MS * 3));
    expect(rankCalls).toHaveLength(3); // no poll outlives the scan
    box.unmount();
  });

  test("a refused scan hands the folder to the live walk", async () => {
    scanReply = { started: false, why: "refused" };
    const box = await search("widget");
    await flush(() => rankCalls[0].reply.resolve(
      answer({ covered: false, reason: "uncovered" })));
    await flush(() => {});
    expect(walkCalls).toEqual(["/d"]);
    box.unmount();
  });
});

describe("what the rows are allowed to arm", () => {
  test("a poll tick does not make the rows on screen stop answering the query", async () => {
    // THE regression: `rowsAnswerQuery` gates auto-selection, and clearing it
    // for the length of every round trip made the listing withdraw and
    // re-place the selection ~20 times during a 30s scan — remounting the
    // preview iframe each time, and leaving Enter a no-op in between.
    const box = await search("widget");
    await flush(() => rankCalls[0].reply.resolve(
      answer({ covered: true, reason: "scanning", hits: [hit("a/widget.md")], total: 1 })));
    expect(box.current().rowsAnswerQuery).toBe(true);

    // the poll issues its next request: the rows still answer "widget"
    await flush(() => clock.advance(SCAN_POLL_MS));
    expect(rankCalls).toHaveLength(2);
    expect(box.current().rowsAnswerQuery).toBe(true);

    await flush(() => rankCalls[1].reply.resolve(
      answer({ hits: [hit("a/widget.md")], total: 1 })));
    expect(box.current().rowsAnswerQuery).toBe(true);
    box.unmount();
  });

  test("rows for a query the user has typed past do NOT answer it", async () => {
    const box = await search("read");
    await flush(() => rankCalls[0].reply.resolve(
      answer({ hits: [hit("README.md")], total: 1 })));
    expect(box.current().rowsAnswerQuery).toBe(true);

    await flush(() => box.current().setQuery("readme"));
    await flush(() => clock.advance(200)); // past the trailing debounce
    // the previous query's rows are still on screen — deliberately, the list
    // is never blanked — and they must not arm Enter or auto-selection.
    expect(box.current().displayHits.map((h) => h.entry.rel)).toEqual(["README.md"]);
    expect(box.current().rowsAnswerQuery).toBe(false);

    await flush(() => rankCalls[1].reply.resolve(
      answer({ hits: [hit("readme.md")], total: 1 })));
    expect(box.current().rowsAnswerQuery).toBe(true);
    box.unmount();
  });
});

describe("replies that outlive what they were asked for", () => {
  test("a reply for the previous FOLDER cannot pin the new one to the walk", async () => {
    const box = await search("widget");
    const stale = rankCalls[0];
    box.rerender("/other", 0); // navigate; the hook is not remounted per folder
    await flush(() => {});
    await flush(() => stale.reply.resolve(answer({ covered: false, reason: "mount" })));
    expect(walkCalls).toEqual([]);
    box.unmount();
  });

  test("a reply for the previous folder cannot ask for a scan of it either", async () => {
    const box = await search("widget");
    const stale = rankCalls[0];
    box.rerender("/other", 0);
    await flush(() => {});
    await flush(() => stale.reply.resolve(answer({ covered: false, reason: "uncovered" })));
    expect(scanCalls).toEqual([]);
    box.unmount();
  });
});

describe("running out of patience with a scan", () => {
  test("gives up after a bounded number of TICKS, whatever the replies do", async () => {
    // The ceiling is counted in ticks, not answers, and this is the case that
    // decides it: every reply here is left hanging, as they would be if rank
    // consistently outlasted the poll interval. A ceiling counted in answers
    // is one this loop can starve.
    const box = await search("widget");
    await flush(() => rankCalls[0].reply.resolve(
      answer({ covered: false, reason: "uncovered" })));
    for (let i = 0; i < MAX_SCANNING_POLLS + 2; i++) {
      await flush(() => clock.advance(SCAN_POLL_MS));
    }
    // ...and an uncovered folder that never got covered ends at the walk
    // rather than at "no matches".
    expect(walkCalls).toEqual(["/d"]);
    const ticks = clock.pending;
    await flush(() => clock.advance(SCAN_POLL_MS * 5));
    expect(clock.pending).toBeLessThanOrEqual(ticks); // the loop is over
    box.unmount();
  });

  test("a new query is not judged by the last query's exhausted patience", async () => {
    // `polls` is per folder+generation, so one query that waited out a long
    // scan used to leave the counter at the ceiling: the NEXT query typed in
    // the same folder gave up on its first answer and switched to the walk
    // mid-keystroke.
    const box = await search("widget");
    // Wait out a whole scan's worth of patience, answering every poll.
    for (let i = 0; i < MAX_SCANNING_POLLS + 1; i++) {
      await flush(() => rankCalls[rankCalls.length - 1].reply.resolve(
        answer({ covered: true, reason: "scanning", hits: [hit("a.md")], total: 1 })));
      await flush(() => clock.advance(SCAN_POLL_MS));
    }
    expect(walkCalls).toEqual([]); // covered, so it settles rather than walks

    // A new query, and the scan is still running.
    await flush(() => box.current().setQuery("gadget"));
    await flush(() => clock.advance(200));
    const fresh = rankCalls[rankCalls.length - 1];
    expect(fresh.q).toBe("gadget");
    await flush(() => fresh.reply.resolve(
      answer({ covered: true, reason: "scanning", hits: [hit("b.md")], total: 1 })));
    // It polls for its own answer instead of inheriting a spent ceiling...
    const before = rankCalls.length;
    await flush(() => clock.advance(SCAN_POLL_MS));
    expect(rankCalls.length).toBe(before + 1);
    // ...and it never quietly became a walk.
    expect(walkCalls).toEqual([]);
    box.unmount();
  });
});

describe("a completed scan", () => {
  test("re-asks, so the answer on screen is never captioned stale", async () => {
    const box = await search("widget");
    await flush(() => rankCalls[0].reply.resolve(
      answer({ hits: [hit("a/widget.md")], total: 1 })));
    expect(box.current().behind).toBe(false);

    // a scan completes: lib/index-status turns that into a lifecycle bump
    await flush(() => freshness.noteIndexLifecycle());
    // ...which re-asks, after the same trailing coalesce any other query pays.
    await flush(() => clock.advance(200));
    expect(rankCalls).toHaveLength(2);
    await flush(() => rankCalls[1].reply.resolve(
      answer({ hits: [hit("a/widget.md")], total: 1 })));
    expect(box.current().behind).toBe(false);
    box.unmount();
  });
});
