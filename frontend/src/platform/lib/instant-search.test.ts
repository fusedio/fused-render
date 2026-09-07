// The three pieces that make a per-query round trip feel instant. Shared by
// both search boxes (the home page's and the listing's in-folder one), which
// is why they are tested away from either of them.
import { describe, expect, it } from "bun:test";
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

describe("INSTANT_DEBOUNCE_MS", () => {
  it("is a plain trailing wait now, not a leading-edge threshold", () => {
    // `searchDelay` is gone: it used to return 0 for the first keystroke
    // after a pause (a leading-edge throttle keyed on when the last request
    // was ISSUED), which made the shortest, most expensive query of any run
    // — the first character typed — the one request guaranteed to fire with
    // no delay at all. Every call site now does an unconditional
    // `window.setTimeout(run, INSTANT_DEBOUNCE_MS)` inside an effect whose
    // cleanup clears the pending timer on every dep change, which by itself
    // IS a correct trailing debounce — there is no separate function left
    // here to unit-test. The actual timing (a burst collapsing to one
    // request, a paused keystroke firing after the wait) is exercised
    // against the real effect in FilesHome.render.test.tsx rather than faked
    // in isolation here.
    expect(INSTANT_DEBOUNCE_MS).toBeGreaterThan(0);
    // NOT `toBeLessThan(PENDING_INDICATOR_MS)`: `INSTANT_DEBOUNCE_MS` is 300,
    // chosen for what a sustained typist feels while holding a key down
    // (instant-search.ts's own comment on the constant), and
    // `PENDING_INDICATOR_MS` (200) is unrelated — it times the REQUEST's own
    // round trip once fired, not the debounce before it fires. The two are
    // sequential, not nested, so debounce > indicator is not a contradiction;
    // flagged rather than asserted either way, since neither value is this
    // test's to judge.
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
