// The ranked search box, DRIVEN: a query is typed, a reply lands, the clock
// moves past the poll interval, the folder changes under an outstanding
// request. Everything here is a sequence, which is precisely what the source
// guards next door (index-source.test.ts) could not test.
import { afterEach, beforeEach, describe, expect, mock, test } from "bun:test";
import type { IndexRankResult, Prefs } from "@platform/lib/api";
import { Clock, Deferred, flush, renderHook } from "@apps/explorer/listing/hook-harness";
import { INSTANT_DEBOUNCE_MS } from "@platform/lib/instant-search";

// --- the module boundary ------------------------------------------------------
const rankCalls: {
  root: string;
  q: string;
  ranked: boolean | undefined;
  reply: Deferred<IndexRankResult>;
}[] = [];
const scanCalls: string[] = [];
let scanReply: { started: boolean; why: string } = { started: true, why: "started" };
// The owner's unranked-search preference (D720) — `useRankedSearchEnabled`
// (ranked-search-pref.ts) reads it via `getPrefs`, which this stub answers
// synchronously-resolved rather than deferred: the pref is not this file's
// subject, and every existing test here asserts the FIRST rank call's shape,
// before a real (deferred) GET could ever land anyway.
let prefsRanked = true;

mock.module("@platform/lib/api", () => ({
  indexRank: (root: string, q: string, opts?: { ranked?: boolean }) => {
    const reply = new Deferred<IndexRankResult>();
    rankCalls.push({ root, q, ranked: opts?.ranked, reply });
    return reply.promise;
  },
  requestFolderScan: (path: string) => {
    scanCalls.push(path);
    return Promise.resolve({ ...scanReply, run_id: "r1", root: path });
  },
  getPrefs: () =>
    Promise.resolve({ indexing: { enabled: true, ranked: prefsRanked } } as Prefs),
}));

mock.module("@platform/lib/router", () => ({ replaceSearch: () => {} }));

const { useListingSearch } = await import("@apps/explorer/listing/useListingSearch");
const freshness = await import("@platform/lib/index-freshness");
// Imported directly (not through the `getPrefs` stub above) so a test can
// pin the preference deterministically: the module-level cache in
// ranked-search-pref.ts is process-global (bun runs every test file in one
// process), so relying on the mocked GET alone would make this hook's
// starting value whatever an earlier, unrelated test file last published.
const { publishRankedSearchEnabled } = await import("@apps/explorer/lib/ranked-search-pref");

function answer(over: Partial<IndexRankResult> = {}): IndexRankResult {
  return {
    covered: true,
    reason: "",
    hits: [],
    truncated: false,
    total: 0,
    base: "/d",
    mode: "substring",
    ...over,
  };
}

const hit = (rel: string) => ({ rel, is_dir: false, size: 1, mtime: 1 });

const clock = new Clock();

beforeEach(() => {
  rankCalls.length = 0;
  scanCalls.length = 0;
  scanReply = { started: true, why: "started" };
  prefsRanked = true;
  publishRankedSearchEnabled(true);
  freshness.resetFsMutations();
  clock.install();
});
afterEach(() => clock.restore());

/** Mount the hook and type `q` into it.
 *
 * `useListingSearch` debounces every query — including the first, now that
 * instant-search dropped its leading-edge throttle for a plain trailing
 * debounce — so this helper advances the fake clock past that wait itself.
 * A test driving a SECOND query goes through `setQuery` directly and
 * advances the clock on its own, same as before. */
async function search(q: string, fsPath = "/d") {
  const box = renderHook((p: string, r: number) => useListingSearch(p, r, false), fsPath, 0);
  await flush(() => box.current().setQuery(q));
  await flush(() => clock.advance(INSTANT_DEBOUNCE_MS));
  return box;
}

const SCAN_POLL_MS = 1_500;
const MAX_SCANNING_POLLS = 80;

describe("the MIN_QUERY_CHARS gate", () => {
  test("a single character never fires a request", async () => {
    const box = renderHook((p: string, r: number) => useListingSearch(p, r, false), "/d", 0);
    await flush(() => box.current().setQuery("w"));
    await flush(() => clock.advance(INSTANT_DEBOUNCE_MS));
    expect(rankCalls).toHaveLength(0);
    expect(box.current().searching).toBe(false);
    box.unmount();
  });

  test("the second character crosses the gate and fires one request", async () => {
    const box = renderHook((p: string, r: number) => useListingSearch(p, r, false), "/d", 0);
    await flush(() => box.current().setQuery("w"));
    await flush(() => clock.advance(INSTANT_DEBOUNCE_MS));
    await flush(() => box.current().setQuery("wi"));
    await flush(() => clock.advance(INSTANT_DEBOUNCE_MS));
    expect(rankCalls).toHaveLength(1);
    expect(rankCalls[0].q).toBe("wi");
    expect(box.current().searching).toBe(true);
    box.unmount();
  });
});

describe("one request per query, abortable", () => {
  test("a second keystroke before the debounce fires only ONE request", async () => {
    const box = renderHook((p: string, r: number) => useListingSearch(p, r, false), "/d", 0);
    await flush(() => box.current().setQuery("wi"));
    await flush(() => clock.advance(INSTANT_DEBOUNCE_MS / 2));
    await flush(() => box.current().setQuery("widget"));
    await flush(() => clock.advance(INSTANT_DEBOUNCE_MS));
    expect(rankCalls).toHaveLength(1);
    expect(rankCalls[0].q).toBe("widget");
    box.unmount();
  });

  test("editing the query past a debounced answer issues a fresh request for the new one", async () => {
    const box = await search("widget");
    await flush(() => box.current().setQuery("gadget"));
    await flush(() => clock.advance(INSTANT_DEBOUNCE_MS));
    expect(rankCalls).toHaveLength(2);
    expect(rankCalls[1].q).toBe("gadget");
    box.unmount();
  });
});

describe("never-blank / stale-while-revalidate", () => {
  test("the previous answer's rows stay on screen while the next is in flight", async () => {
    const box = await search("read");
    await flush(() => rankCalls[0].reply.resolve(answer({ hits: [hit("README.md")], total: 1 })));
    expect(box.current().displayHits.map((h) => h.entry.rel)).toEqual(["README.md"]);
    expect(box.current().rowsAnswerQuery).toBe(true);

    await flush(() => box.current().setQuery("readme"));
    await flush(() => clock.advance(INSTANT_DEBOUNCE_MS));
    // Still showing the OLD rows — the list is never blanked — but flagged
    // as not answering the query in the box any more.
    expect(box.current().displayHits.map((h) => h.entry.rel)).toEqual(["README.md"]);
    expect(box.current().rowsAnswerQuery).toBe(false);

    await flush(() => rankCalls[1].reply.resolve(answer({ hits: [hit("readme.md")], total: 1 })));
    expect(box.current().displayHits.map((h) => h.entry.rel)).toEqual(["readme.md"]);
    expect(box.current().rowsAnswerQuery).toBe(true);
    box.unmount();
  });
});

describe("an uncovered folder: scan, poll, answer", () => {
  test("asks for a scan once, keeps saying an answer is coming, then answers", async () => {
    const box = await search("widget");
    expect(rankCalls).toHaveLength(1);

    await flush(() => rankCalls[0].reply.resolve(answer({ covered: false, reason: "uncovered" })));
    expect(scanCalls).toEqual(["/d"]);
    expect(box.current().scanPending).toBe(true);
    expect(box.current().displayHits).toEqual([]);

    await flush(() => clock.advance(SCAN_POLL_MS));
    expect(rankCalls).toHaveLength(2);
    await flush(() => rankCalls[1].reply.resolve(answer({ covered: true, reason: "scanning" })));
    expect(box.current().scanPending).toBe(true);
    expect(scanCalls).toHaveLength(1); // asked ONCE, however many polls

    await flush(() => clock.advance(SCAN_POLL_MS));
    await flush(() => clock.advance(INSTANT_DEBOUNCE_MS));
    await flush(() => rankCalls[2].reply.resolve(answer({ hits: [hit("a/widget.md")], total: 1 })));
    expect(box.current().displayHits.map((h) => h.entry.rel)).toEqual(["a/widget.md"]);
    expect(box.current().scanPending).toBe(false);

    await flush(() => clock.advance(SCAN_POLL_MS * 3));
    expect(rankCalls).toHaveLength(3); // no poll outlives the scan
    box.unmount();
  });

  test("a scan request that fails to start still lets polling stop and settle", async () => {
    scanReply = { started: false, why: "refused" };
    const box = await search("widget");
    await flush(() => rankCalls[0].reply.resolve(answer({ covered: false, reason: "uncovered" })));
    await flush(() => {});
    expect(box.current().scanPending).toBe(false);
    box.unmount();
  });
});

describe("an uncoverable folder reports the index gap, not an infinite loop", () => {
  test("a mount-backed folder answers immediately with its reason, and never polls", async () => {
    const box = await search("widget");
    await flush(() => rankCalls[0].reply.resolve(answer({ covered: false, reason: "mount" })));
    expect(box.current().scanPending).toBe(false);
    expect(box.current().reason).toBe("mount");
    expect(scanCalls).toEqual([]);

    // Nothing polls for it: no further request appears on its own.
    await flush(() => clock.advance(SCAN_POLL_MS * 5));
    expect(rankCalls).toHaveLength(1);
    box.unmount();
  });

  test("a folder that stays uncovered after being scanned settles at the index gap", async () => {
    const box = await search("widget");
    await flush(() => rankCalls[0].reply.resolve(answer({ covered: false, reason: "uncovered" })));
    expect(scanCalls).toEqual(["/d"]);

    // Grace polls, each still uncovered.
    for (let i = 0; i < 5; i++) {
      await flush(() => clock.advance(SCAN_POLL_MS));
      await flush(() => clock.advance(INSTANT_DEBOUNCE_MS));
      const last = rankCalls[rankCalls.length - 1];
      await flush(() => last.reply.resolve(answer({ covered: false, reason: "uncovered" })));
    }
    // It gives up rather than looping forever, and reports the reason.
    expect(box.current().scanPending).toBe(false);
    expect(box.current().reason).toBe("uncovered");
    expect(scanCalls).toHaveLength(1); // never asked twice
    box.unmount();
  });

  test("running out of poll patience on a scanning folder settles too", async () => {
    // Ticks count against the ceiling whatever the replies do — every one of
    // these is left hanging, as they would be if rank consistently outlasted
    // the poll interval. A ceiling counted in answers is one this loop could
    // starve.
    const box = await search("widget");
    await flush(() => rankCalls[0].reply.resolve(answer({ covered: false, reason: "uncovered" })));
    for (let i = 0; i < MAX_SCANNING_POLLS + 2; i++) {
      await flush(() => clock.advance(SCAN_POLL_MS));
    }
    const ticks = clock.pending;
    await flush(() => clock.advance(SCAN_POLL_MS * 5));
    expect(clock.pending).toBeLessThanOrEqual(ticks); // the poll loop is over
    box.unmount();
  });
});

describe("replies that outlive what they were asked for", () => {
  test("a reply for the previous FOLDER cannot ask for a scan of the new one", async () => {
    const box = await search("widget");
    const stale = rankCalls[0];
    box.rerender("/other", 0); // navigate; the hook is not remounted per folder
    await flush(() => {});
    await flush(() => stale.reply.resolve(answer({ covered: false, reason: "uncovered" })));
    expect(scanCalls).toEqual([]);
    box.unmount();
  });
});

describe("an answer served from the memo", () => {
  test("is captioned by the generation it was FETCHED under", async () => {
    const box = await search("alpha");
    await flush(() => rankCalls[0].reply.resolve(answer({ hits: [hit("alpha.md")], total: 1 })));
    expect(box.current().behind).toBe(false);

    box.rerender("/d", 2);
    await flush(() => {});
    expect(box.current().behind).toBe(true);

    await flush(() => box.current().setQuery("zeta"));
    await flush(() => clock.advance(INSTANT_DEBOUNCE_MS));
    await flush(() => rankCalls[rankCalls.length - 1].reply.resolve(
      answer({ hits: [hit("zeta.md")], total: 1 })));
    expect(box.current().behind).toBe(false);

    const asked = rankCalls.length;
    await flush(() => box.current().setQuery("alpha"));
    await flush(() => clock.advance(INSTANT_DEBOUNCE_MS));
    expect(rankCalls.length).toBe(asked); // answered from memory
    expect(box.current().displayHits.map((h) => h.entry.rel)).toEqual(["alpha.md"]);
    expect(box.current().behind).toBe(true);
    box.unmount();
  });
});

describe("a completed scan", () => {
  test("re-asks, so the answer on screen is never captioned stale", async () => {
    const box = await search("widget");
    await flush(() => rankCalls[0].reply.resolve(answer({ hits: [hit("a/widget.md")], total: 1 })));
    expect(box.current().behind).toBe(false);

    await flush(() => freshness.noteIndexLifecycle());
    await flush(() => clock.advance(INSTANT_DEBOUNCE_MS));
    expect(rankCalls).toHaveLength(2);
    await flush(() => rankCalls[1].reply.resolve(answer({ hits: [hit("a/widget.md")], total: 1 })));
    expect(box.current().behind).toBe(false);
    box.unmount();
  });
});

describe("the ranked-search preference (D720)", () => {
  test("a user query sends `ranked` reflecting the current preference", async () => {
    const box = await search("widget");
    expect(rankCalls).toHaveLength(1);
    expect(rankCalls[0].ranked).toBe(true);
    box.unmount();
  });

  test("turning ranking off changes what the next request sends", async () => {
    publishRankedSearchEnabled(false);
    const box = await search("widget");
    expect(rankCalls).toHaveLength(1);
    expect(rankCalls[0].ranked).toBe(false);
    box.unmount();
  });
});

describe("decision 4: a path/pattern query waits for Enter", () => {
  test("typing a glob fires no request until commitSearch is called", async () => {
    const box = renderHook((p: string, r: number) => useListingSearch(p, r, false), "/d", 0);
    await flush(() => box.current().setQuery("a/*.py"));
    await flush(() => clock.advance(INSTANT_DEBOUNCE_MS));
    expect(rankCalls).toHaveLength(0);
    expect(box.current().searching).toBe(true);
    expect(box.current().searchState.status).toBe("idle");

    await flush(() => box.current().commitSearch());
    await flush(() => clock.advance(INSTANT_DEBOUNCE_MS));
    expect(rankCalls).toHaveLength(1);
    expect(rankCalls[0].q).toBe("a/*.py");
    box.unmount();
  });

  test("plain text never gates — it still fires on every debounced keystroke", async () => {
    const box = await search("widget");
    expect(rankCalls).toHaveLength(1);
    box.unmount();
  });

  test("editing a committed glob further re-gates until the next commit", async () => {
    const box = renderHook((p: string, r: number) => useListingSearch(p, r, false), "/d", 0);
    await flush(() => box.current().setQuery("a/*.py"));
    await flush(() => box.current().commitSearch());
    await flush(() => clock.advance(INSTANT_DEBOUNCE_MS));
    expect(rankCalls).toHaveLength(1);
    await flush(() => rankCalls[0].reply.resolve(answer({ hits: [hit("a/x.py")], total: 1 })));

    await flush(() => box.current().setQuery("a/*.pyc"));
    await flush(() => clock.advance(INSTANT_DEBOUNCE_MS));
    // No new request yet — the previous answer stays on screen, stale.
    expect(rankCalls).toHaveLength(1);
    expect(box.current().displayHits.map((h) => h.entry.rel)).toEqual(["a/x.py"]);
    expect(box.current().rowsAnswerQuery).toBe(false);

    await flush(() => box.current().commitSearch());
    await flush(() => clock.advance(INSTANT_DEBOUNCE_MS));
    expect(rankCalls).toHaveLength(2);
    expect(rankCalls[1].q).toBe("a/*.pyc");
    box.unmount();
  });
});

describe("closing the box mid-scan", () => {
  test("does not carry spent patience into the next search", async () => {
    const box = await search("widget");
    await flush(() => rankCalls[0].reply.resolve(answer({ covered: false, reason: "uncovered" })));
    for (let i = 0; i < 50; i++) {
      await flush(() => clock.advance(SCAN_POLL_MS));
      const last = rankCalls[rankCalls.length - 1];
      await flush(() => last.reply.resolve(answer({ covered: false, reason: "scanning" })));
    }
    // Escape, and back.
    await flush(() => box.current().setQuery(""));
    await flush(() => box.current().setQuery("widget"));
    await flush(() => clock.advance(INSTANT_DEBOUNCE_MS));

    for (let i = 0; i < 35; i++) {
      await flush(() => clock.advance(SCAN_POLL_MS));
      const last = rankCalls[rankCalls.length - 1];
      await flush(() => last.reply.resolve(answer({ covered: false, reason: "scanning" })));
    }
    // 35 ticks after a reset is nowhere near a ceiling of 80 — still polling.
    expect(box.current().scanPending).toBe(true);
    box.unmount();
  });
});
