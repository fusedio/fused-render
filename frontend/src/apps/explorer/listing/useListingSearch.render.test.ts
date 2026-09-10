// The ranked search box, DRIVEN: a query is typed, a reply lands, the clock
// moves past the poll interval, the folder changes under an outstanding
// request. Everything here is a sequence, which is precisely what the source
// guards next door (index-source.test.ts) could not test.
import { afterEach, beforeEach, describe, expect, mock, test } from "bun:test";
import type { IndexRankResult, Prefs } from "@platform/lib/api";
import { Clock, Deferred, flush, renderHook } from "@apps/explorer/listing/hook-harness";
import { INSTANT_DEBOUNCE_MS } from "@platform/lib/instant-search";
import { searchCaveat } from "@apps/explorer/listing/index-caveat";

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

// `navHintQCommitted` is read at mount by `useListingSearch` itself (the
// already-committed-query seed for a navigation that arrived from the file
// view's merged field) — omitting it here throws `SyntaxError: Export named
// 'navHintQCommitted' not found` at import time and fails this WHOLE FILE to
// load, silently voiding every describe block below (including "a
// path-shaped query never asks the index"). No test in this file exercises
// that seeding path, so a plain `false` (every mount behaves like a fresh
// load) is enough.
mock.module("@platform/lib/router", () => ({
  replaceSearch: () => {},
  navHintQCommitted: () => false,
}));

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
  const box = renderHook((p: string, r: number) => useListingSearch(p, undefined, r, false), fsPath, 0);
  await flush(() => box.current().setQuery(q));
  await flush(() => clock.advance(INSTANT_DEBOUNCE_MS));
  return box;
}

const SCAN_POLL_MS = 1_500;
const MAX_SCANNING_POLLS = 80;

describe("the MIN_QUERY_CHARS gate", () => {
  test("a single character never fires a request", async () => {
    const box = renderHook((p: string, r: number) => useListingSearch(p, undefined, r, false), "/d", 0);
    await flush(() => box.current().setQuery("w"));
    await flush(() => clock.advance(INSTANT_DEBOUNCE_MS));
    expect(rankCalls).toHaveLength(0);
    expect(box.current().searching).toBe(false);
    box.unmount();
  });

  test("the second character crosses the gate and fires one request", async () => {
    const box = renderHook((p: string, r: number) => useListingSearch(p, undefined, r, false), "/d", 0);
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
    const box = renderHook((p: string, r: number) => useListingSearch(p, undefined, r, false), "/d", 0);
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

describe("a request that fails with rows already on screen", () => {
  test("the old rows stay, but the hook stops calling them a healthy answer", async () => {
    const box = await search("foo");
    await flush(() =>
      rankCalls[0].reply.resolve(answer({ hits: [hit("foo.txt")], total: 1, base: "/d" })),
    );
    expect(box.current().displayHits.map((h) => h.entry.rel)).toEqual(["foo.txt"]);
    expect(box.current().behind).toBe(false);
    expect(box.current().requestFailed).toBe(false);

    await flush(() => box.current().setQuery("foobar"));
    await flush(() => clock.advance(INSTANT_DEBOUNCE_MS));
    await flush(() => rankCalls[1].reply.reject(new Error("network error")));

    // The rows from "foo" are still the ones on screen — never blanked —
    // but a failed request for "foobar" must not read as a settled, healthy
    // answer to it: `behind` (and the new `requestFailed`) flip true so the
    // caveat chip can say the search itself failed, not merely "not
    // refreshed".
    expect(box.current().displayHits.map((h) => h.entry.rel)).toEqual(["foo.txt"]);
    expect(box.current().requestFailed).toBe(true);
    expect(box.current().behind).toBe(true);
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

  test("an escaping query asks for a scan of the resolved base, not the open folder", async () => {
    const box = renderHook((p: string, r: number) => useListingSearch(p, undefined, r, false), "/d", 0);
    await flush(() => box.current().setQuery("~/other/widget"));
    await flush(() => box.current().commitSearch());
    await flush(() => clock.advance(INSTANT_DEBOUNCE_MS));
    await flush(() =>
      rankCalls[0].reply.resolve(
        answer({ covered: false, reason: "uncovered", base: "/home/u/other" }),
      ),
    );
    expect(scanCalls).toEqual(["/home/u/other"]);
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

describe("behind requires an actual answer, not just a generation mismatch", () => {
  test("a first-ever query in a bumped generation reports behind === false", async () => {
    const box = await search("widget");
    // No answer has ever landed for this search — the request is still
    // outstanding — so a generation bump here must not read as "not
    // refreshed": there is nothing on screen for that caption to describe.
    box.rerender("/d", 2);
    await flush(() => {});
    expect(box.current().behind).toBe(false);

    // Once an answer for THIS generation actually lands, behind stays false.
    await flush(() => rankCalls[0].reply.resolve(answer({ hits: [hit("widget.md")], total: 1 })));
    expect(box.current().behind).toBe(false);
    box.unmount();
  });
});

describe("a dir-watch bump while searching: the deferral itself is the caveat", () => {
  test("no re-ask is scheduled, so the caveat still has to show on its own", async () => {
    const box = await search("widget");
    await flush(() => rankCalls[0].reply.resolve(answer({ hits: [hit("widget.md")], total: 1 })));
    expect(box.current().behind).toBe(false);

    // A dir-watch refresh (listing/revalidate's `shouldReconcile`) is
    // deliberately NOT a reason to re-ask while a search is active — the
    // fetch effect keys on `pinned`, not `gen`, so this alone never
    // reschedules anything.
    box.rerender("/d", 1);
    await flush(() => {});
    expect(box.current().behind).toBe(true);
    expect(rankCalls).toHaveLength(1);
    expect(box.current().requestComing).toBe(false);
    // Nothing is coming, so the caveat has to carry the whole claim on its
    // own — this is the genuine "not refreshed" case, not the round-trip gap
    // the fix above closes.
    expect(
      searchCaveat(null, {
        behind: box.current().behind,
        pending: box.current().requestComing,
        rescanPending: false,
      }),
    ).not.toBeNull();
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

describe("a URL-restored search racing the app's own startup scan", () => {
  test("a lifecycle bump reschedules the debounce without ever letting the caveat show", async () => {
    // `searching` true on the very first render, the way a `?q=` mount seeds
    // it — no explicit `setQuery` after the harness has settled.
    (globalThis as Record<string, unknown>).location = { search: "?q=widget", pathname: "/x" };
    const box = renderHook((p: string, r: number) => useListingSearch(p, undefined, r, true), "/d", 0);
    await flush(() => clock.advance(INSTANT_DEBOUNCE_MS));
    expect(rankCalls).toHaveLength(1);
    await flush(() => rankCalls[0].reply.resolve(answer({ hits: [hit("a/widget.md")], total: 1 })));
    expect(box.current().behind).toBe(false);

    // The generation moves — the same event `describe("a completed scan")`
    // above drives — which reschedules the debounced re-ask.
    await flush(() => freshness.noteIndexLifecycle());
    // The gap: `behind` already reads true (the answer on screen was fetched
    // under the old generation), and the debounce this bump just rescheduled
    // has not fired yet. The caveat must not show here — nothing is stuck,
    // an answer is already coming.
    expect(box.current().behind).toBe(true);
    expect(
      searchCaveat(null, {
        behind: box.current().behind,
        pending: box.current().requestComing,
        rescanPending: false,
      }),
    ).toBeNull();

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

describe("decision 4: a query that escapes the box root waits for Enter", () => {
  test("typing a query with a leading ~ fires no request until commitSearch is called", async () => {
    const box = renderHook((p: string, r: number) => useListingSearch(p, undefined, r, false), "/d", 0);
    await flush(() => box.current().setQuery("~/a/*.py"));
    await flush(() => clock.advance(INSTANT_DEBOUNCE_MS));
    expect(rankCalls).toHaveLength(0);
    expect(box.current().searching).toBe(true);
    expect(box.current().searchState.status).toBe("idle");

    await flush(() => box.current().commitSearch());
    await flush(() => clock.advance(INSTANT_DEBOUNCE_MS));
    expect(rankCalls).toHaveLength(1);
    expect(rankCalls[0].q).toBe("~/a/*.py");
    box.unmount();
  });

  test("plain text never gates — it still fires on every debounced keystroke", async () => {
    const box = await search("widget");
    expect(rankCalls).toHaveLength(1);
    box.unmount();
  });

  test("a glob anchored at the box root never gates either", async () => {
    const box = renderHook((p: string, r: number) => useListingSearch(p, undefined, r, false), "/d", 0);
    await flush(() => box.current().setQuery("a/*.py"));
    await flush(() => clock.advance(INSTANT_DEBOUNCE_MS));
    expect(rankCalls).toHaveLength(1);
    expect(rankCalls[0].q).toBe("a/*.py");
    box.unmount();
  });

  test("editing a committed escaping query further re-gates until the next commit", async () => {
    const box = renderHook((p: string, r: number) => useListingSearch(p, undefined, r, false), "/d", 0);
    await flush(() => box.current().setQuery("~/a/*.py"));
    await flush(() => box.current().commitSearch());
    await flush(() => clock.advance(INSTANT_DEBOUNCE_MS));
    expect(rankCalls).toHaveLength(1);
    await flush(() => rankCalls[0].reply.resolve(answer({ hits: [hit("a/x.py")], total: 1 })));

    await flush(() => box.current().setQuery("~/a/*.pyc"));
    await flush(() => clock.advance(INSTANT_DEBOUNCE_MS));
    // No new request yet — the previous answer stays on screen, stale.
    expect(rankCalls).toHaveLength(1);
    expect(box.current().displayHits.map((h) => h.entry.rel)).toEqual(["a/x.py"]);
    expect(box.current().rowsAnswerQuery).toBe(false);
    // The rows on screen answer the last COMMITTED query, not this edit —
    // that is `awaitingCommit`, not `behind`. Nothing failed to refresh;
    // nothing has been asked for the query now in the box.
    expect(box.current().awaitingCommit).toBe(true);
    expect(box.current().behind).toBe(false);

    await flush(() => box.current().commitSearch());
    await flush(() => clock.advance(INSTANT_DEBOUNCE_MS));
    expect(rankCalls).toHaveLength(2);
    expect(rankCalls[1].q).toBe("~/a/*.pyc");
    box.unmount();
  });
});

describe("a path-shaped query never asks the index (path-shaped-query.ts)", () => {
  test("a real, existing-shaped absolute path fires no rank request even after Enter", async () => {
    const box = renderHook((p: string, r: number) => useListingSearch(p, undefined, r, false), "/d", 0);
    await flush(() => box.current().setQuery("/other/folder"));
    await flush(() => clock.advance(INSTANT_DEBOUNCE_MS));
    expect(box.current().searching).toBe(true);
    expect(box.current().isPathQuery).toBe(true);
    expect(rankCalls).toHaveLength(0);

    // Escaping (a leading "/") normally waits for Enter — committing it must
    // not somehow unlock a request that isPathQuery has already ruled out.
    await flush(() => box.current().commitSearch());
    await flush(() => clock.advance(INSTANT_DEBOUNCE_MS));
    expect(rankCalls).toHaveLength(0);
    expect(box.current().searchState.status).toBe("idle");
    expect(box.current().displayHits).toEqual([]);
    box.unmount();
  });

  test("a partial prefix of a real path is path-shaped too — shape only, never existence", async () => {
    const box = renderHook((p: string, r: number) => useListingSearch(p, undefined, r, false), "/d", 0);
    await flush(() => box.current().setQuery("/other/fol"));
    await flush(() => clock.advance(INSTANT_DEBOUNCE_MS));
    expect(box.current().isPathQuery).toBe(true);
    expect(rankCalls).toHaveLength(0);
    box.unmount();
  });

  test("the same query WITH a glob still searches — a glob is never path-shaped", async () => {
    const box = renderHook((p: string, r: number) => useListingSearch(p, undefined, r, false), "/d", 0);
    await flush(() => box.current().setQuery("/other/fol*"));
    await flush(() => box.current().commitSearch());
    await flush(() => clock.advance(INSTANT_DEBOUNCE_MS));
    expect(box.current().isPathQuery).toBe(false);
    expect(rankCalls).toHaveLength(1);
    box.unmount();
  });

  test("a bare filter word is not path-shaped and still searches", async () => {
    const box = await search("widget");
    expect(box.current().isPathQuery).toBe(false);
    expect(rankCalls).toHaveLength(1);
    box.unmount();
  });

  // Finding 2 (code review): the "Path" rule is scoped to ABSOLUTE-ish
  // queries only. A relative slash-bearing query like "src/util" must NOT be
  // treated as path-shaped — it still live-filters the subtree via a rank
  // request, exactly as it did before this predicate existed.
  test("a relative slash-bearing query is not path-shaped and still searches", async () => {
    const box = await search("src/util");
    expect(box.current().isPathQuery).toBe(false);
    expect(rankCalls).toHaveLength(1);
    expect(rankCalls[0].q).toBe("src/util");
    box.unmount();
  });

  // Finding 1 (code review): a path-shaped query never gets a rank answer, so
  // `navRows` in Listing.tsx falls back to the FOLDER's own rows (per
  // `showsSearchHits`) rather than search hits. With no lead selected, the
  // document Enter handler (useListingSelection.ts) opens `rows[0]` unless
  // `rowsAnswerQuery` says not to — and those folder rows never answer a
  // path-shaped query typed in the box, existing or not. Before this fix
  // `rowsAnswerQuery` was `!runsSearch || …`, which read `true` for every
  // path-shaped query (runsSearch is false for all of them), so Enter with
  // nothing selected would open an arbitrary unrelated folder row.
  //
  // The original repro (code review) used a RELATIVE path-shaped query
  // ("docs/rea") — relative slash-bearing queries no longer count as
  // path-shaped after finding 2's narrowing (see the "Path means
  // absolute-ish shape" describe block below), so this uses an absolute one
  // instead; the underlying `rowsAnswerQuery` bug is the same either way.
  test("rowsAnswerQuery is false for a path-shaped query — Enter must not open an arbitrary folder row", async () => {
    const box = renderHook((p: string, r: number) => useListingSearch(p, undefined, r, false), "/d", 0);
    await flush(() => box.current().setQuery("/other/fol"));
    await flush(() => clock.advance(INSTANT_DEBOUNCE_MS));
    expect(box.current().searching).toBe(true);
    expect(box.current().isPathQuery).toBe(true);
    expect(box.current().rowsAnswerQuery).toBe(false);
    box.unmount();
  });

  test("rowsAnswerQuery stays true for an empty box — plain folder browsing is unaffected", async () => {
    const box = renderHook((p: string, r: number) => useListingSearch(p, undefined, r, false), "/d", 0);
    expect(box.current().searching).toBe(false);
    expect(box.current().rowsAnswerQuery).toBe(true);
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

describe("decision 10: elapsedMs is the true round-trip, and a held answer keeps it", () => {
  test("measures issue-to-apply, not just the debounce wait", async () => {
    const box = await search("widget");
    // The debounce has already elapsed (search() advances it); this models
    // the server itself taking 250ms to reply.
    await flush(() => clock.advance(250));
    await flush(() => rankCalls[0].reply.resolve(answer({ hits: [hit("widget.md")], total: 1 })));
    const state = box.current().searchState;
    expect(state.status).toBe("ok");
    if (state.status === "ok") expect(state.elapsedMs).toBe(250);
    box.unmount();
  });

  test("a memoized reply keeps the elapsedMs it was measured with, not ~0ms", async () => {
    const box = await search("alpha");
    await flush(() => clock.advance(400));
    await flush(() => rankCalls[0].reply.resolve(answer({ hits: [hit("alpha.md")], total: 1 })));
    const first = box.current().searchState;
    expect(first.status).toBe("ok");
    if (first.status === "ok") expect(first.elapsedMs).toBe(400);

    // A second, different query — a real request, answered quickly.
    await flush(() => box.current().setQuery("beta"));
    await flush(() => clock.advance(INSTANT_DEBOUNCE_MS));
    await flush(() =>
      rankCalls[rankCalls.length - 1].reply.resolve(
        answer({ hits: [hit("beta.md")], total: 1 }),
      ),
    );
    const asked = rankCalls.length;

    // Back to "alpha" — served from the memo, no new request.
    await flush(() => box.current().setQuery("alpha"));
    await flush(() => clock.advance(INSTANT_DEBOUNCE_MS));
    expect(rankCalls.length).toBe(asked);

    const replay = box.current().searchState;
    expect(replay.status).toBe("ok");
    // Still 400ms — the real cost of the request that actually ran, not the
    // ~0ms a re-timed cache hit would report.
    if (replay.status === "ok") expect(replay.elapsedMs).toBe(400);
    box.unmount();
  });
});

describe("searchBase: the directory hits are relative to", () => {
  test("is the box's own root when nothing is searching", async () => {
    const box = renderHook((p: string, r: number) => useListingSearch(p, undefined, r, false), "/proj", 0);
    expect(box.current().searchBase).toBe("/proj");
    box.unmount();
  });

  test("follows the server's resolved base once an answer lands, not fsPath", async () => {
    // A `~`/`/`-leading query can walk the resolved base out past the
    // folder being searched (fused_render/index/query.py's resolve_query) —
    // callers building a row path from `entry.rel` must join onto the
    // server's `base`, not onto `/proj`. Path-shaped, so it waits for an
    // explicit commit (decision 4).
    const box = renderHook((p: string, r: number) => useListingSearch(p, undefined, r, false), "/proj", 0);
    await flush(() => box.current().setQuery("~/other/rep"));
    await flush(() => box.current().commitSearch());
    await flush(() => clock.advance(INSTANT_DEBOUNCE_MS));
    await flush(() =>
      rankCalls[0].reply.resolve(
        answer({ base: "/home/u/other", hits: [hit("report.csv")], total: 1 }),
      ),
    );
    expect(box.current().searchBase).toBe("/home/u/other");
    box.unmount();
  });

  test("names nothing while the very first request for an escaping query is still out", async () => {
    // Before ANY answer has landed, `/proj` (the folder already open) is not
    // this request's base — a leading `~` is written specifically to escape
    // it — so naming `/proj` here would be the header claiming a base this
    // search does not have. Empty is what the header renders as a bare
    // "Path", not a wrong or stale answer.
    const box = renderHook((p: string, r: number) => useListingSearch(p, undefined, r, false), "/proj", 0);
    await flush(() => box.current().setQuery("~/other/rep"));
    await flush(() => box.current().commitSearch());
    await flush(() => clock.advance(INSTANT_DEBOUNCE_MS));
    expect(rankCalls).toHaveLength(1);
    expect(box.current().searchBase).toBe("");

    await flush(() =>
      rankCalls[0].reply.resolve(
        answer({ base: "/home/u/other", hits: [hit("report.csv")], total: 1 }),
      ),
    );
    // One transition, straight to the real base — never a detour through
    // `/proj` first.
    expect(box.current().searchBase).toBe("/home/u/other");
    box.unmount();
  });
});
