// The three pieces that make a per-query round trip feel instant. Shared by
// both search boxes (the home page's and the listing's in-folder one), which
// is why they are tested away from either of them.
import { describe, expect, it } from "bun:test";
import { Clock } from "@apps/explorer/listing/hook-harness";
import {
  INSTANT_DEBOUNCE_MS,
  PENDING_INDICATOR_MS,
  QUERY_MEMO_LIMIT,
  QueryMemo,
  STALE_CLEAR_MS,
} from "@platform/lib/instant-search";

interface Answer {
  query: string;
  total: number;
}

const answer = (over: Partial<Answer> = {}): Answer => ({
  query: "read",
  total: 1,
  ...over,
});

// A minimal stand-in for what every call site actually does with
// `INSTANT_DEBOUNCE_MS` — `window.setTimeout(run, INSTANT_DEBOUNCE_MS)`,
// with the previous pending timer cleared on every new keystroke (the same
// shape an effect's cleanup gives it) — so the debounce SHAPE itself is
// exercised against the real constant rather than duplicated by re-mounting
// FilesHome or useWalkSearch here. Those two files still own the "this wired
// into the actual box" coverage (FilesHome.render.test.tsx); this is the
// "this constant, used the documented way, behaves like a trailing debounce"
// coverage, which nothing here asserted before.
function debouncer(fired: string[]): (query: string) => void {
  let timer: number | null = null;
  return (query: string) => {
    if (timer !== null) window.clearTimeout(timer);
    timer = window.setTimeout(() => fired.push(query), INSTANT_DEBOUNCE_MS);
  };
}

describe("INSTANT_DEBOUNCE_MS", () => {
  it("collapses a burst of keystrokes into one request, for the LAST query typed", () => {
    const clock = new Clock();
    clock.install();
    try {
      const fired: string[] = [];
      const type = debouncer(fired);
      type("r");
      clock.advance(50);
      type("re");
      clock.advance(50);
      type("read");
      // Well inside the debounce window of the last keystroke: nothing fired
      // yet, and definitely not the earlier partial queries.
      clock.advance(INSTANT_DEBOUNCE_MS - 1);
      expect(fired).toEqual([]);
      clock.advance(1);
      expect(fired).toEqual(["read"]);
    } finally {
      clock.restore();
    }
  });

  it("still waits the FULL debounce for a single keystroke after a long pause", () => {
    // The leading-edge throttle this replaced fired the first keystroke
    // after a pause with NO delay — backwards, because that keystroke is
    // always the shortest, broadest, most expensive query of the run. This
    // pins that a keystroke arriving after an arbitrarily long idle period
    // gets no special treatment: it waits the same full debounce as every
    // other one.
    const clock = new Clock();
    clock.install();
    try {
      const fired: string[] = [];
      const type = debouncer(fired);
      clock.advance(10_000);
      type("x");
      clock.advance(INSTANT_DEBOUNCE_MS - 1);
      expect(fired).toEqual([]);
      clock.advance(1);
      expect(fired).toEqual(["x"]);
    } finally {
      clock.restore();
    }
  });

  it("is a positive wait, and not nested inside PENDING_INDICATOR_MS", () => {
    expect(INSTANT_DEBOUNCE_MS).toBeGreaterThan(0);
    // NOT `toBeLessThan(PENDING_INDICATOR_MS)`: `PENDING_INDICATOR_MS` times
    // the REQUEST's own round trip once fired, not the debounce before it
    // fires — the two are sequential, not nested, so debounce > indicator is
    // not a contradiction; flagged rather than asserted either way, since
    // neither value is this test's to judge.
  });
});

describe("QueryMemo", () => {
  it("answers a repeated query without a round trip", () => {
    // Backspacing walks back through queries just answered; re-asking the
    // server for those is a wait the user can feel for rows already in hand.
    const memo = new QueryMemo<Answer>();
    const a = answer({ query: "read" });
    memo.put("read", a);
    expect(memo.get("read")).toBe(a);
    expect(memo.get("reader")).toBeUndefined();
  });

  it("drops the OLDEST entry past the limit", () => {
    const memo = new QueryMemo<Answer>(3);
    for (const q of ["a", "ab", "abc", "abcd"]) memo.put(q, answer({ query: q }));
    expect(memo.size).toBe(3);
    expect(memo.get("a")).toBeUndefined();
    expect(memo.get("abcd")).toBeDefined();
  });

  it("refreshes an entry that is put again, rather than aging it out", () => {
    const memo = new QueryMemo<Answer>(2);
    memo.put("a", answer({ query: "a" }));
    memo.put("b", answer({ query: "b" }));
    memo.put("a", answer({ query: "a", total: 2 }));
    memo.put("c", answer({ query: "c" }));
    expect(memo.get("b")).toBeUndefined();
    expect(memo.get("a")?.total).toBe(2);
  });

  it("clears wholesale, which is how an index lifecycle change is handled", () => {
    // A scan finishing makes every remembered answer suspect at once; there is
    // no per-entry story to tell.
    const memo = new QueryMemo<Answer>();
    memo.put("a", answer());
    memo.clear();
    expect(memo.size).toBe(0);
  });

  it("defaults to a small trail, not a cache", () => {
    expect(QUERY_MEMO_LIMIT).toBe(20);
  });
});

describe("STALE_CLEAR_MS", () => {
  it("gives a request longer to answer than the pending indicator does", () => {
    // Admitting a wait is happening (PENDING_INDICATOR_MS) is a much cheaper
    // decision than throwing away the rows on screen (STALE_CLEAR_MS) — the
    // second has to wait longer than the first, or a request that is merely
    // slow (already past PENDING_INDICATOR_MS) would immediately also count
    // as stale.
    expect(STALE_CLEAR_MS).toBeGreaterThan(PENDING_INDICATOR_MS);
  });
});
