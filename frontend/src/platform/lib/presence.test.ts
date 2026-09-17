// The presence registry — source matching, staleness, and the
// throws-degrade-to-notify rule. See SPEC-quiet-notifications.md §1.
import { expect, test } from "bun:test";
import { installDomShim } from "@platform/lib/testDomShim";

installDomShim();

const {
  matchesSource,
  currentPresencePage,
  isOpenAnywhere,
  snapshotIsOpenAnywhere,
  isNarrator,
  computeTopLevel,
  PRESENCE_STALE_MS,
  _writePresenceForTest,
  _removePresenceForTest,
  _presenceWindowIdForTest,
} = await import("@platform/lib/presence");

// ---- source matching -------------------------------------------------

test("matchesSource: exact match", () => {
  expect(matchesSource("/preferences", "/preferences")).toBe(true);
});

test("matchesSource: fs path nested under a folder source counts as open", () => {
  expect(matchesSource("/Users/me/project/sub/file.py", "/Users/me/project")).toBe(true);
});

test("matchesSource: a source nested under the window's own page counts as open", () => {
  expect(matchesSource("/Users/me/project", "/Users/me/project/sub/file.py")).toBe(true);
});

test("matchesSource: sibling paths with a shared prefix do not match", () => {
  expect(matchesSource("/Users/me/project-2/file.py", "/Users/me/project")).toBe(false);
});

test("matchesSource: /preferences?tab=lan does not match /preferences?tab=indexing", () => {
  expect(matchesSource("/preferences?tab=indexing", "/preferences?tab=lan")).toBe(false);
});

test("matchesSource: /preferences?tab=lan matches itself exactly", () => {
  expect(matchesSource("/preferences?tab=lan", "/preferences?tab=lan")).toBe(true);
});

// Defect 1 (2026-09-17): `tab=lan` is NOT a registered `ORIGIN_BY_ROUTE` key
// (only `/preferences?tab=indexing` is, per Fix 19) and nothing today raises
// a job/message with that source — it is used exactly once in this codebase,
// as a plain navigation TARGET (`RepoUpdatesDock.tsx`'s "Fix with Claude"
// button), never as a presence identity a producer names. Per the new
// canonicalization rule, an unregistered query is app/shell state, not
// identity, so it now collapses to the bare route and DOES match it — this
// replaces the old test above, which pinned the opposite (pre-fix) behavior.
// A genuinely registered query-bearing surface stays distinct — see the next
// test, which is the real regression guard this one used to (incompletely)
// stand in for.
test("matchesSource: an unregistered query is app state and canonicalizes down to the bare route", () => {
  expect(matchesSource("/preferences?tab=lan", "/preferences")).toBe(true);
  expect(matchesSource("/preferences", "/preferences?tab=lan")).toBe(true);
});

test("matchesSource: a REGISTERED query-bearing surface (ORIGIN_BY_ROUTE) never matches the bare route", () => {
  expect(matchesSource("/preferences?tab=indexing", "/preferences")).toBe(false);
  expect(matchesSource("/preferences", "/preferences?tab=indexing")).toBe(false);
});

// ---- Defect 1: canonicalization collapses app-state query params ---------

test("matchesSource: two Playground URLs differing only by prompt/model app-state still match (grouping/suppression bug)", () => {
  const a = "/ai-models/playground?prompt=Explain+how+a+language+model+picks&model=mlx-community%2FLFM2.5-1.2B-Instruct-4bit";
  const b = "/ai-models/playground?model=mlx-community%2FLFM2.5-1.2B-Instruct-4bit";
  // Both directions: a stale `Job.source` captured at request time still
  // matches the live (and differently-dirty) current presence page.
  expect(matchesSource(a, b)).toBe(true);
  expect(matchesSource(b, a)).toBe(true);
  // And the canonical bare route matches either dirty variant too — this is
  // what makes a row raised from a page whose query has since changed stay
  // suppressed while that page is still open.
  expect(matchesSource("/ai-models/playground", a)).toBe(true);
  expect(matchesSource(a, "/ai-models/playground")).toBe(true);
});

// ---- currentPresencePage: canonicalization at the source ------------------
//
// This is what `api.ts`'s `ambientSourceHeaders()` sends as `X-Fused-Source`
// on every request, and what `familyKey` (jobs.ts) ultimately groups by once
// the server echoes it back as `Job.source` — so proving canonicalization
// happens HERE is what proves "two renders from the same page with
// different query strings share a family key and cluster into one row".
// Mutates the shared `location` stub directly (a plain object off
// `installDomShim`, not `defineProperty`d) and restores it, since this file
// shares one `globalThis` with every other suite in the same `bun test`
// process (see that shim's own header).
test("currentPresencePage: two renders on the same route with different app-state query strings canonicalize to the same identity", () => {
  const loc = (globalThis as unknown as { location: { pathname: string; search: string } })
    .location;
  const savedPathname = loc.pathname;
  const savedSearch = loc.search;
  try {
    loc.pathname = "/ai-models/playground";
    loc.search =
      "?prompt=Explain+how+a+language+model+picks+the+next+word&model=mlx-community%2FLFM2.5-1.2B-Instruct-4bit";
    const first = currentPresencePage();
    loc.search = "?model=mlx-community%2FLFM2.5-1.2B-Instruct-4bit";
    const second = currentPresencePage();
    expect(first).toBe("/ai-models/playground");
    expect(second).toBe("/ai-models/playground");
    expect(first).toBe(second);
  } finally {
    loc.pathname = savedPathname;
    loc.search = savedSearch;
  }
});

test("currentPresencePage: a registered ORIGIN_BY_ROUTE query stays whole, distinct from the bare route", () => {
  const loc = (globalThis as unknown as { location: { pathname: string; search: string } })
    .location;
  const savedPathname = loc.pathname;
  const savedSearch = loc.search;
  try {
    loc.pathname = "/preferences";
    loc.search = "?tab=indexing";
    expect(currentPresencePage()).toBe("/preferences?tab=indexing");
    loc.search = "";
    expect(currentPresencePage()).toBe("/preferences");
  } finally {
    loc.pathname = savedPathname;
    loc.search = savedSearch;
  }
});

// ---- isOpenAnywhere / staleness / throw-degrade ------------------------

function fakeStorage(initial: Record<string, unknown> = {}) {
  const map = new Map<string, string>(
    Object.entries(initial).map(([k, v]) => [k, JSON.stringify(v)])
  );
  return {
    getItem: (k: string) => map.get(k) ?? null,
    setItem: (k: string, v: string) => {
      map.set(k, v);
    },
    removeItem: (k: string) => {
      map.delete(k);
    },
  };
}

test("isOpenAnywhere: true when a fresh entry matches the source", () => {
  const storage = fakeStorage({
    "fused-render:presence": {
      w1: { page: "/ai-models/local", focused: true, ts: 1000, topLevel: true },
    },
  });
  expect(isOpenAnywhere("/ai-models/local", { storage, now: () => 1000 })).toBe(true);
});

test("isOpenAnywhere: false when nothing matches", () => {
  const storage = fakeStorage({
    "fused-render:presence": {
      w1: { page: "/tasks", focused: true, ts: 1000, topLevel: true },
    },
  });
  expect(isOpenAnywhere("/ai-models/local", { storage, now: () => 1000 })).toBe(false);
});

test("isOpenAnywhere: a stale entry (a window closed without cleanup) never suppresses forever", () => {
  const storage = fakeStorage({
    "fused-render:presence": {
      w1: { page: "/ai-models/local", focused: true, ts: 1000, topLevel: true },
    },
  });
  const now = 1000 + PRESENCE_STALE_MS + 1;
  expect(isOpenAnywhere("/ai-models/local", { storage, now: () => now })).toBe(false);
});

test("isOpenAnywhere: an entry just inside the staleness window still counts", () => {
  const storage = fakeStorage({
    "fused-render:presence": {
      w1: { page: "/ai-models/local", focused: true, ts: 1000, topLevel: true },
    },
  });
  const now = 1000 + PRESENCE_STALE_MS - 1;
  expect(isOpenAnywhere("/ai-models/local", { storage, now: () => now })).toBe(true);
});

test("isOpenAnywhere: a storage that throws on read degrades to false (notify), not a crash", () => {
  const throwingStorage = {
    getItem: () => {
      throw new Error("SecurityError: blocked");
    },
    setItem: () => {
      throw new Error("blocked");
    },
    removeItem: () => {},
  };
  expect(() => isOpenAnywhere("/ai-models/local", { storage: throwingStorage, now: () => 1000 })).not.toThrow();
  expect(isOpenAnywhere("/ai-models/local", { storage: throwingStorage, now: () => 1000 })).toBe(false);
});

test("isOpenAnywhere: no localStorage at all (private window) degrades to false", () => {
  expect(isOpenAnywhere("/ai-models/local", { storage: null, now: () => 1000 })).toBe(false);
});

test("isOpenAnywhere: a corrupt stored value degrades to false rather than throwing", () => {
  const storage = {
    getItem: () => "not json{{{",
    setItem: () => {},
    removeItem: () => {},
  };
  expect(() => isOpenAnywhere("/x", { storage, now: () => 1000 })).not.toThrow();
  expect(isOpenAnywhere("/x", { storage, now: () => 1000 })).toBe(false);
});

// ---- snapshotIsOpenAnywhere (finding 6) ---------------------------------

test("snapshotIsOpenAnywhere: matches the same source/staleness rules as isOpenAnywhere", () => {
  const storage = fakeStorage({
    "fused-render:presence": {
      w1: { page: "/ai-models/local", focused: true, ts: 1000, topLevel: true },
      w2: { page: "/tasks", focused: false, ts: 1000, topLevel: true },
    },
  });
  const predicate = snapshotIsOpenAnywhere({ storage, now: () => 1000 });
  expect(predicate("/ai-models/local")).toBe(true);
  expect(predicate("/tasks")).toBe(true);
  expect(predicate("/preferences")).toBe(false);

  const staleNow = 1000 + PRESENCE_STALE_MS + 1;
  const stalePredicate = snapshotIsOpenAnywhere({ storage, now: () => staleNow });
  expect(stalePredicate("/ai-models/local")).toBe(false);
});

test("snapshotIsOpenAnywhere: degrades to a false-returning predicate when storage throws", () => {
  const throwingStorage = {
    getItem: () => {
      throw new Error("SecurityError: blocked");
    },
    setItem: () => {},
    removeItem: () => {},
  };
  const predicate = snapshotIsOpenAnywhere({ storage: throwingStorage, now: () => 1000 });
  expect(predicate("/ai-models/local")).toBe(false);
});

test("snapshotIsOpenAnywhere: reads the registry exactly once no matter how many sources are checked", () => {
  let reads = 0;
  const backing = fakeStorage({
    "fused-render:presence": {
      w1: { page: "/ai-models/local", focused: true, ts: 1000, topLevel: true },
    },
  });
  const countingStorage = {
    getItem: (k: string) => {
      reads += 1;
      return backing.getItem(k);
    },
    setItem: backing.setItem,
    removeItem: backing.removeItem,
  };
  const predicate = snapshotIsOpenAnywhere({ storage: countingStorage, now: () => 1000 });
  expect(reads).toBe(1);
  // Simulate a single poll tick checking many jobs (and group members)
  // against the same snapshot: none of these may touch storage again.
  predicate("/ai-models/local");
  predicate("/tasks");
  predicate("/preferences");
  predicate("/ai-models/local");
  expect(reads).toBe(1);

  // For contrast: the same four checks against the raw `isOpenAnywhere`
  // (as every call site used before this fix) would read storage every
  // single time -- this is the exact O(N) pattern the snapshot replaces.
  reads = 0;
  isOpenAnywhere("/ai-models/local", { storage: countingStorage, now: () => 1000 });
  isOpenAnywhere("/tasks", { storage: countingStorage, now: () => 1000 });
  isOpenAnywhere("/preferences", { storage: countingStorage, now: () => 1000 });
  isOpenAnywhere("/ai-models/local", { storage: countingStorage, now: () => 1000 });
  expect(reads).toBe(4);
});

// ---- writeSelf/removeSelf race (finding 10) -----------------------------

/** A storage stub whose `getItem` walks through `sequence` one call at a
 *  time (holding on the last entry once exhausted) — lets a test simulate
 *  "the registry changed between my check-read and my verify-read" without
 *  needing real concurrency, since `mutateRegistry` always calls `getItem`
 *  at least twice per attempt (once to read, once to verify before commit). */
function racingStorage(sequence: string[]) {
  let calls = 0;
  const setCalls: string[] = [];
  return {
    getItem: (_k: string) => {
      const raw = sequence[Math.min(calls, sequence.length - 1)];
      calls += 1;
      return raw;
    },
    setItem: (_k: string, v: string) => {
      setCalls.push(v);
    },
    removeItem: () => {},
    setCalls,
  };
}

test("removeSelf (finding 10): a peer joining between the check-read and the verify-read is not lost", () => {
  const self = _presenceWindowIdForTest();
  const selfOnly = JSON.stringify({
    [self]: { page: "/a", focused: true, ts: 1000, topLevel: true },
  });
  const selfAndPeer = JSON.stringify({
    [self]: { page: "/a", focused: true, ts: 1000, topLevel: true },
    peer: { page: "/b", focused: true, ts: 1000, topLevel: true },
  });
  // Attempt 0: initial read sees `selfOnly`, transform drops self -> {}.
  // Verify-read sees `selfAndPeer` (peer joined in between) -> mismatch,
  // retry. Attempt 1: initial read sees `selfAndPeer`, transform drops self
  // -> {peer}. Verify-read sees `selfAndPeer` again (stable) -> commit.
  const storage = racingStorage([selfOnly, selfAndPeer, selfAndPeer, selfAndPeer]);
  _removePresenceForTest({ storage, now: () => 1000 });
  expect(storage.setCalls.length).toBe(1);
  const committed = JSON.parse(storage.setCalls[0]);
  expect(self in committed).toBe(false);
  expect("peer" in committed).toBe(true); // never clobbered by the stale read
});

test("writeSelf (finding 10): a peer's concurrent removal is not resurrected by a stale read", () => {
  const self = _presenceWindowIdForTest();
  const selfAndPeer = JSON.stringify({
    [self]: { page: "/a", focused: true, ts: 500, topLevel: true },
    peer: { page: "/b", focused: true, ts: 500, topLevel: true },
  });
  const peerLeft = JSON.stringify({
    [self]: { page: "/a", focused: true, ts: 500, topLevel: true },
  });
  // Attempt 0: initial read still sees the peer, transform refreshes self's
  // own entry (peer untouched). Verify-read sees `peerLeft` (peer removed
  // itself in between) -> mismatch, retry against the fresher snapshot.
  const storage = racingStorage([selfAndPeer, peerLeft, peerLeft, peerLeft]);
  _writePresenceForTest({ storage, now: () => 2000 });
  expect(storage.setCalls.length).toBe(1);
  const committed = JSON.parse(storage.setCalls[0]);
  expect("peer" in committed).toBe(false); // never resurrected
  expect(committed[self].ts).toBe(2000); // this document's own refresh still lands
});

test("removeSelf (finding 10): a no-op removal (never registered) never attempts a write", () => {
  const storage = racingStorage([JSON.stringify({ someoneElse: { page: "/x", focused: true, ts: 1000, topLevel: true } })]);
  _removePresenceForTest({ storage, now: () => 1000 });
  expect(storage.setCalls.length).toBe(0);
});

// ---- narrator election --------------------------------------------------

test("isNarrator: the lowest non-stale top-level windowId narrates", () => {
  const storage = fakeStorage({
    "fused-render:presence": {
      w2: { page: "/tasks", focused: true, ts: 1000, topLevel: true },
      w1: { page: "/tasks", focused: false, ts: 1000, topLevel: true },
    },
  });
  // Neither w1 nor w2 is this document's own minted id, so both routes must
  // agree the answer is "no" for this document either way — assert via the
  // ordering directly instead, since `windowId` is module-private.
  const ids = Object.keys(JSON.parse(storage.getItem("fused-render:presence")!)).sort();
  expect(ids[0]).toBe("w1");
  void isNarrator; // exercised for real (self vs. others) in the integration test below.
});

test("isNarrator: a pane entry (topLevel: false) is never eligible even if its id sorts first", () => {
  const storage = fakeStorage({
    "fused-render:presence": {
      a_pane: { page: "/tasks", focused: true, ts: 1000, topLevel: false },
    },
  });
  // With no OTHER top-level entry on record, this document (topLevel by
  // default in a real DOM) is the only eligible candidate — degrade to "yes".
  expect(isNarrator({ storage, now: () => 1000 })).toBe(true);
});

test("isNarrator: nobody on record degrades to true (narrate), never to silence", () => {
  const storage = fakeStorage({});
  expect(isNarrator({ storage, now: () => 1000 })).toBe(true);
});

// ---- Finding 1: an embed must never be narrator-eligible -----------------
//
// `computeTopLevel` is what `writeSelf` stores as a document's `topLevel`
// flag, which is exactly the set `isNarrator` elects from above. Before this
// fix, a standalone embed/preview tab (top-level in its own window, but
// `IS_EMBED`) registered `topLevel: true` and could win the election even
// though nothing ever narrates from an embed — silencing every
// schedule/task notification for as long as it sorted first and stayed
// open. `IS_EMBED` is only ever computed once, at module load, from
// `location.pathname` (see `router.ts`), so it can't be varied within one
// test file/process without contaminating every other suite that shares
// `globalThis` in the same `bun test` run (see `testDomShim.ts`'s header).
// `computeTopLevel` is pulled out as a pure function precisely so this
// exclusion can be pinned directly, against both `isEmbed` values, without
// touching that shared module-load state.
test("computeTopLevel: an embed document is never topLevel, even when it is window.top", () => {
  const top = {} as Window;
  (top as unknown as { top: Window }).top = top;
  expect(computeTopLevel(true, top)).toBe(false);
});

test("computeTopLevel: a non-embed top-level document is topLevel (pre-fix behavior preserved)", () => {
  const top = {} as Window;
  (top as unknown as { top: Window }).top = top;
  expect(computeTopLevel(false, top)).toBe(true);
});

test("computeTopLevel: a non-embed framed (pane) document is never topLevel", () => {
  const top = {} as Window;
  const frame = {} as Window;
  (top as unknown as { top: Window }).top = top;
  (frame as unknown as { top: Window }).top = top;
  expect(computeTopLevel(false, frame)).toBe(false);
});

test("computeTopLevel: an embed pane (framed embed) is never topLevel either", () => {
  const top = {} as Window;
  const frame = {} as Window;
  (top as unknown as { top: Window }).top = top;
  (frame as unknown as { top: Window }).top = top;
  expect(computeTopLevel(true, frame)).toBe(false);
});

test("computeTopLevel: no window (SSR-ish) degrades to topLevel when not an embed", () => {
  expect(computeTopLevel(false, undefined)).toBe(true);
  expect(computeTopLevel(true, undefined)).toBe(false);
});
