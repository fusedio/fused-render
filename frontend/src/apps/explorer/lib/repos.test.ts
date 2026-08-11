import { describe, expect, it } from "bun:test";
import {
  refreshIsPending,
  reposMessage,
  reposNeedsIndexPoll,
  reposStaleNote,
  reposView,
  type ReposInputs,
  type ReposView,
} from "@apps/explorer/lib/repos";
import type { GitRepos } from "@platform/lib/api";

const msg = (v: ReposView) => reposMessage(v);

const res = (over: Partial<GitRepos> = {}): GitRepos => ({
  indexed: false,
  scanning: false,
  stale: false,
  repos: [],
  ...over,
});

/** The view, with every input defaulted to "nothing happening". */
const view = (over: Partial<ReposInputs> = {}): ReposView =>
  reposView({
    response: null,
    failed: false,
    liveScanning: null,
    refreshPending: false,
    ...over,
  });

const REPOS = [{ path: "/a" }, { path: "/b" }];
const LIST = res({ indexed: true, repos: REPOS });
const STALE_LIST = res({ indexed: true, stale: true, repos: REPOS });

// -- resting states ----------------------------------------------------------

describe("reposView resting states", () => {
  it("is loading until the first response, which is not the same as empty", () => {
    expect(view()).toEqual({ kind: "loading" });
    expect(msg(view())).toMatch(/Looking for repos/);
  });

  it("reports a failed FIRST fetch", () => {
    expect(view({ failed: true })).toEqual({ kind: "failed" });
  });

  it("an indexed answer is ready, empty list included", () => {
    const v = view({ response: res({ indexed: true }) });
    expect(v).toEqual({ kind: "ready", repos: [], stale: false });
    expect(msg(v)).toMatch(/No git repositories/);
  });

  it("only 'unavailable' is actionable, and only when nothing is in motion", () => {
    expect(view({ response: res({ reason: "no-index" }) }).kind).toBe("unavailable");
    expect(msg(view({ response: res({ reason: "no-index" }) }))).toMatch(/Preferences/);
    // the other no-list states must never send the user anywhere
    for (const v of [
      view({ response: res({ reason: "no-index" }), liveScanning: true }),
      view({ response: res({ reason: "no-index" }), refreshPending: true }),
      view({ response: res({ reason: "outdated" }) }),
    ]) {
      expect(msg(v)).not.toMatch(/Preferences/);
    }
  });

  it("gives every state distinct non-empty copy, so none falls through", () => {
    const all: ReposView[] = [
      { kind: "loading" },
      { kind: "failed" },
      { kind: "building" },
      { kind: "unavailable" },
      { kind: "outdated" },
      { kind: "ready", repos: [], stale: false },
    ];
    const seen = all.map(msg);
    expect(seen.every((m) => m.length > 0)).toBe(true);
    expect(new Set(seen).size).toBe(all.length);
  });
});

// -- the two invariants ------------------------------------------------------

describe("NEVER REGRESS: a list on screen is not taken away", () => {
  it("survives a failed refresh", () => {
    const v = view({ response: LIST, failed: true });
    expect(v).toEqual({ kind: "ready", repos: REPOS, stale: false });
  });

  it("survives a rescan, gaining only a note", () => {
    const v = view({ response: LIST, liveScanning: true });
    expect(v).toEqual({ kind: "ready", repos: REPOS, stale: true });
    expect(reposStaleNote(v)).toMatch(/may be out of date/i);
  });

  it("survives a pending refresh without flashing the note", () => {
    // `refreshPending` deliberately does NOT set `stale`: it would blink a note on
    // every refetch of a perfectly fresh list.
    const v = view({ response: LIST, refreshPending: true });
    expect(v).toEqual({ kind: "ready", repos: REPOS, stale: false });
    expect(reposStaleNote(v)).toBeNull();
  });
});

describe("NEVER ACTIONABLE while something is coming", () => {
  it("a pending refresh holds 'building' instead of 'go rebuild it'", () => {
    // Finding 7, twice over. When a first scan ends, the poll flips to idle before
    // the keyed refetch lands; without `refreshPending` this cell rendered the
    // actionable copy for one round trip.
    const v = view({
      response: res({ reason: "no-index", scanning: true }),
      liveScanning: false,
      refreshPending: true,
    });
    expect(v.kind).toBe("building");
    expect(msg(v)).not.toMatch(/Preferences/);
  });

  it("only reaches 'unavailable' once the refresh has landed and nothing runs", () => {
    const v = view({ response: res({ reason: "no-index" }), liveScanning: false });
    expect(v.kind).toBe("unavailable");
  });
});

// -- transitions: where all five bugs lived ----------------------------------

describe("transitions", () => {
  it("SCAN STARTS over an empty index: unavailable -> building", () => {
    const before = view({ response: res({ reason: "no-index" }), liveScanning: false });
    expect(before.kind).toBe("unavailable");
    // the poll sees a scan; the response has not changed yet
    const after = view({
      response: res({ reason: "no-index" }),
      liveScanning: true,
      refreshPending: true,
    });
    expect(after.kind).toBe("building");
  });

  it("SCAN ENDS, refetch pending: stays 'building' for the whole gap", () => {
    const during = view({
      response: res({ reason: "no-index", scanning: true }),
      liveScanning: true,
    });
    expect(during.kind).toBe("building");
    // poll idle, new answer not yet applied -> must NOT become actionable
    const gap = view({
      response: res({ reason: "no-index", scanning: true }),
      liveScanning: false,
      refreshPending: true,
    });
    expect(gap.kind).toBe("building");
  });

  it("REFETCH LANDS FRESH: building -> ready, no note", () => {
    const v = view({ response: LIST, liveScanning: false, refreshPending: false });
    expect(v).toEqual({ kind: "ready", repos: REPOS, stale: false });
    expect(reposStaleNote(v)).toBeNull();
  });

  it("REFETCH LANDS STILL-STALE (partial multi-root): ready + note, keeps polling", () => {
    const v = view({ response: STALE_LIST, liveScanning: false });
    expect(v).toEqual({ kind: "ready", repos: REPOS, stale: true });
    expect(reposStaleNote(v)).not.toBeNull();
    // Finding 8: this is the state that used to freeze the poll and strand the note.
    expect(reposNeedsIndexPoll(STALE_LIST)).toBe(true);
  });

  it("SCAN CANCELLED with a usable list on screen: cards never disappear", () => {
    const during = view({ response: STALE_LIST, liveScanning: true });
    expect(during.kind).toBe("ready");
    // cancelled: poll idle, manifest untouched, refetch in flight
    const gap = view({ response: STALE_LIST, liveScanning: false, refreshPending: true });
    expect(gap).toEqual({ kind: "ready", repos: REPOS, stale: true });
    // ...and the refetch returns the same still-stale answer. Still a list.
    const after = view({ response: STALE_LIST, liveScanning: false });
    expect(after.kind).toBe("ready");
    expect((after as { repos: unknown }).repos).toEqual(REPOS);
  });

  it("SCAN CANCELLED with NOTHING on screen: reaches the actionable state", () => {
    // The counterpart that must still work: a cancelled first scan leaves no index,
    // and now nothing is coming, so the user does need to be told.
    const v = view({ response: res({ reason: "no-index" }), liveScanning: false });
    expect(v.kind).toBe("unavailable");
    expect(msg(v)).toMatch(/Preferences/);
  });
});

// -- the poll gate -----------------------------------------------------------

describe("reposNeedsIndexPoll", () => {
  it("polls until the answer is FINAL, not merely present", () => {
    expect(reposNeedsIndexPoll(null)).toBe(true);              // nothing yet
    expect(reposNeedsIndexPoll(res({ reason: "no-index" }))).toBe(true);
    expect(reposNeedsIndexPoll(STALE_LIST)).toBe(true);        // finding 8
    expect(reposNeedsIndexPoll(res({ indexed: true, scanning: true, stale: true })))
      .toBe(true);
    expect(reposNeedsIndexPoll(LIST)).toBe(false);             // fresh: stop
  });
});

// -- refreshIsPending: the mount corner --------------------------------------

describe("refreshIsPending", () => {
  it("is false before anything has been fetched", () => {
    expect(refreshIsPending(undefined, null)).toBe(false);
    expect(refreshIsPending(undefined, "false|1")).toBe(false);
  });

  it("treats the FIRST poll reading as a baseline, not a change", () => {
    // The bug: at mount the held response is fetched before any poll, so
    // fetchedKey is null; the first reading then flipped the key and claimed a
    // pending refresh, flashing "Still building…" over an idle tab's CTA.
    expect(refreshIsPending(null, "false|123")).toBe(false);
    expect(refreshIsPending(null, null)).toBe(false);
  });

  it("is true for every later change, which is what fixes the scan-end flicker", () => {
    expect(refreshIsPending("false|1", "true|1")).toBe(true);   // scan started
    expect(refreshIsPending("true|1", "false|1")).toBe(true);   // cancelled/failed
    expect(refreshIsPending("true|1", "false|2")).toBe(true);   // completed
    expect(refreshIsPending("false|1", "false|1")).toBe(false); // nothing changed
  });
});

describe("the mount sequence, step by step", () => {
  // An IDLE machine with no index: the tab must reach its actionable CTA and stay
  // there, never passing through "Still building…".
  const idleNoIndex = res({ indexed: false, scanning: false, reason: "no-index" });

  it("loading -> unavailable, with no building flash in between", () => {
    // 1. mounted, nothing fetched, no poll yet
    let key: string | null = null;
    let fetched: string | null | undefined = undefined;
    expect(
      reposView({ response: null, failed: false, liveScanning: null,
                  refreshPending: refreshIsPending(fetched, key) }).kind,
    ).toBe("loading");

    // 2. first response lands (fetched before any poll reading)
    fetched = null;
    expect(
      reposView({ response: idleNoIndex, failed: false, liveScanning: null,
                  refreshPending: refreshIsPending(fetched, key) }).kind,
    ).toBe("unavailable");

    // 3. the first poll reading arrives: idle, agreeing with the response. THIS is
    //    the step that used to flash "building" for a redundant round trip.
    key = "false|999";
    const v = reposView({ response: idleNoIndex, failed: false, liveScanning: false,
                          refreshPending: refreshIsPending(fetched, key) });
    expect(v.kind).toBe("unavailable");
    expect(msg(v)).toMatch(/Preferences/);

    // 4. the redundant refetch lands with the same answer. Still settled.
    fetched = key;
    expect(
      reposView({ response: idleNoIndex, failed: false, liveScanning: false,
                  refreshPending: refreshIsPending(fetched, key) }).kind,
    ).toBe("unavailable");
  });

  it("an outdated index reaches its own message without a building flash", () => {
    const outdated = res({ indexed: false, scanning: false, reason: "outdated" });
    const v = reposView({ response: outdated, failed: false, liveScanning: false,
                          refreshPending: refreshIsPending(null, "false|1") });
    expect(v.kind).toBe("outdated");
  });

  it("but a scan starting AFTER the baseline still reaches building, no CTA flash", () => {
    // The case that needs `refreshPending`: the held response predates the scan
    // entirely, so its own `scanning` is false and only pending covers the gap.
    const fetched = "false|1";
    // scan runs
    expect(
      reposView({ response: idleNoIndex, failed: false, liveScanning: true,
                  refreshPending: refreshIsPending(fetched, "true|1") }).kind,
    ).toBe("building");
    // scan ends; poll idle again, refetch in flight -> must NOT show the CTA
    const gap = reposView({
      response: idleNoIndex, failed: false, liveScanning: false,
      refreshPending: refreshIsPending(fetched, "false|2"),
    });
    expect(gap.kind).toBe("building");
    expect(msg(gap)).not.toMatch(/Preferences/);
  });
});

// -- the whole table, walked ------------------------------------------------
//
// Every input combination, checked against the two invariants and for total
// coverage. This is the test that replaces "I reasoned about the cells": five bugs
// in this file were all a cell nobody enumerated.
describe("the complete input space", () => {
  const responses: Array<GitRepos | null> = [
    null,
    res({ reason: "no-index" }),
    res({ reason: "no-index", scanning: true }),
    res({ reason: "outdated" }),
    res({ indexed: true }),
    LIST,
    STALE_LIST,
  ];
  // refreshPending is walked as the KEY PAIR it is derived from, not as a free
  // boolean: the mount corner (fetchedKey null + a first reading) is a specific
  // pair, and enumerating the boolean alone is what let it through inspection.
  const keyPairs: Array<[string | null | undefined, string | null]> = [
    [undefined, null],      // mounted, nothing fetched, no poll
    [undefined, "false|1"], // nothing fetched, poll already read
    [null, null],           // response held, poll never read
    [null, "false|1"],      // THE mount corner: first reading arrives
    ["false|1", "false|1"], // settled
    ["false|1", "true|1"],  // a scan started
    ["true|1", "false|1"],  // cancelled or failed
    ["true|1", "false|2"],  // completed
  ];
  const cells: Array<{ input: ReposInputs; v: ReposView }> = [];
  for (const response of responses) {
    for (const failed of [false, true]) {
      for (const liveScanning of [null, false, true]) {
        for (const [fetchedKey, indexKey] of keyPairs) {
          const input = {
            response,
            failed,
            liveScanning,
            refreshPending: refreshIsPending(fetchedKey, indexKey),
          };
          cells.push({ input, v: reposView(input) });
        }
      }
    }
  }

  it("is total: every cell yields one of the known kinds", () => {
    const kinds = new Set([
      "loading", "failed", "building", "unavailable", "outdated", "ready",
    ]);
    for (const { v } of cells) expect(kinds.has(v.kind)).toBe(true);
  });

  it("NEVER REGRESS holds in every cell: an answer is always shown", () => {
    for (const { input, v } of cells) {
      if (input.response?.indexed) {
        expect(v.kind).toBe("ready");
        expect((v as { repos: unknown }).repos).toEqual(input.response.repos);
      }
    }
  });

  it("NEVER ACTIONABLE holds in every cell where something is coming", () => {
    for (const { input, v } of cells) {
      const scanning = input.liveScanning ?? input.response?.scanning ?? false;
      if (scanning || input.refreshPending) {
        expect(msg(v)).not.toMatch(/Preferences/);
      }
    }
  });

  it("NO FALSE BUILDING: a settled idle load never claims a scan", () => {
    // The #2 invariant, by construction. When the response itself reports idle,
    // the live poll agrees, and the only key movement is the mount baseline, there
    // is nothing in motion — so `building` would be a claim about nothing and
    // would hide the CTA the user actually needs.
    for (const [fetchedKey, indexKey] of keyPairs) {
      if (refreshIsPending(fetchedKey, indexKey)) continue;
      for (const response of responses) {
        if (response === null || response.indexed || response.scanning) continue;
        const v = reposView({ response, failed: false, liveScanning: false,
                              refreshPending: false });
        expect(v.kind).not.toBe("building");
      }
    }
  });

  it("every state is reachable — no dead branch in the union", () => {
    const reached = new Set(cells.map((c) => c.v.kind));
    expect([...reached].sort()).toEqual([
      "building", "failed", "loading", "outdated", "ready", "unavailable",
    ]);
  });

  it("the poll stops in exactly one situation: a fresh, present answer", () => {
    for (const response of responses) {
      const shouldStop = response !== null && response.indexed && !response.stale;
      expect(reposNeedsIndexPoll(response)).toBe(!shouldStop);
    }
  });
});

// -- zero rows: missing data vs a real answer --------------------------------

describe("zero rows", () => {
  it("an index predating repo detection is NOT 'no repositories'", () => {
    const v = view({ response: res({ reason: "outdated" }), liveScanning: false });
    expect(v.kind).toBe("outdated");
    expect(msg(v)).not.toMatch(/No git repositories/);
  });

  it("a fresh index that found nothing IS 'no repositories'", () => {
    expect(msg(view({ response: res({ indexed: true }) }))).toMatch(/No git repositories/);
  });
});
