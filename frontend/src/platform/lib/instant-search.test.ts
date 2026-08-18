// The three pieces that make a per-query round trip feel instant. Shared by
// both search boxes (the home page's and the listing's in-folder one), which
// is why they are tested away from either of them.
import { describe, expect, it } from "bun:test";
import {
  INSTANT_DEBOUNCE_MS,
  QUERY_MEMO_LIMIT,
  QueryMemo,
  searchDelay,
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

describe("searchDelay", () => {
  it("is ZERO on the leading edge — the first keystroke does not wait", () => {
    // The debounce coalesces fast typing; it is not a delay on the first
    // request. A selective query answers in ~40ms and must not sit behind a
    // timer, or the box feels hesitant while doing less work than before.
    expect(searchDelay(10_000, 0)).toBe(0);
    expect(searchDelay(10_000, 10_000 - INSTANT_DEBOUNCE_MS)).toBe(0);
  });

  it("waits out the REMAINDER of the window during a burst", () => {
    // Not a fresh full window per keystroke: a fast typist's requests land one
    // debounce apart rather than one per letter.
    expect(searchDelay(10_000, 9_960)).toBe(INSTANT_DEBOUNCE_MS - 40);
    expect(searchDelay(10_000, 10_000)).toBe(INSTANT_DEBOUNCE_MS);
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
