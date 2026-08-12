import { describe, expect, it } from "bun:test";
import { corpusKey, nextHeldCorpus, scannableCorpus } from "@apps/explorer/listing/corpus-hold";
import type { WalkState } from "@apps/explorer/listing/types";
import type { WalkEntry } from "@platform/lib/api";

function entries(...rels: string[]): WalkEntry[] {
  return rels.map((rel) => ({ rel, is_dir: false, size: 1, mtime: 2 }));
}

const A = entries("a.txt", "b.txt");
const B = entries("c.txt");

function streaming(es: WalkEntry[], key = "k1", forRefresh = 0): WalkState {
  return { status: "streaming", entries: es, count: es.length, key, forRefresh };
}
function ok(es: WalkEntry[], key = "k1", forRefresh = 0): WalkState {
  return { status: "ok", entries: es, truncated: false, total: es.length, key, forRefresh };
}

describe("corpusKey", () => {
  it("is the same corpus for the same folder, generation and source", () => {
    expect(corpusKey("walk", "/p", 3, 0)).toBe(corpusKey("walk", "/p", 3, 0));
  });

  it("separates a RETRY from the attempt it replaces", () => {
    // The bug: a retry keeps the folder and the generation (only `retryNonce`
    // moves), so a walk that streamed 500 entries and then failed had those
    // 500 hits resumed over a brand-new walk's array. A retry usually happens
    // because something changed, which is exactly when those rows differ.
    expect(corpusKey("walk", "/p", 3, 1)).not.toBe(corpusKey("walk", "/p", 3, 0));
  });

  it("separates the two racing sources, which do not return the same rows", () => {
    expect(corpusKey("index", "/p", 3, 0)).not.toBe(corpusKey("walk", "/p", 3, 0));
  });

  it("separates folders and generations", () => {
    expect(corpusKey("walk", "/q", 3, 0)).not.toBe(corpusKey("walk", "/p", 3, 0));
    expect(corpusKey("walk", "/p", 4, 0)).not.toBe(corpusKey("walk", "/p", 3, 0));
  });
});

describe("nextHeldCorpus", () => {
  it("retains a settled corpus", () => {
    expect(nextHeldCorpus(ok(A), null)).toEqual({ entries: A, key: "k1" });
  });

  it("retains a partial streamed corpus too — some rows beat none", () => {
    expect(nextHeldCorpus(streaming(A), null)).toEqual({ entries: A, key: "k1" });
  });

  it("keeps the previous hold when the walk is invalidated to idle", () => {
    const held = nextHeldCorpus(ok(A), null);
    expect(nextHeldCorpus({ status: "idle" }, held)).toBe(held!);
  });

  it("keeps the previous hold while the new walk has nothing yet", () => {
    const held = nextHeldCorpus(ok(A), null);
    expect(nextHeldCorpus(streaming([], "k2", 1), held)).toBe(held!);
  });

  it("is identity-stable across re-renders of the same corpus", () => {
    const held = nextHeldCorpus(ok(A), null);
    expect(nextHeldCorpus(ok(A), held)).toBe(held!);
  });

  it("adopts the new generation once it has entries", () => {
    const held = nextHeldCorpus(ok(A), null);
    expect(nextHeldCorpus(ok(B, "k2", 1), held)).toEqual({ entries: B, key: "k2" });
  });
});

describe("scannableCorpus", () => {
  it("scans nothing outside search", () => {
    expect(scannableCorpus(false, ok(A), { entries: A, key: "k1" }).entries).toBeNull();
  });

  it("scans the current corpus when it has one", () => {
    expect(scannableCorpus(true, ok(A), null)).toEqual({ entries: A, key: "k1", stale: false });
  });

  it("scans a settled EMPTY corpus rather than the hold — that is a real answer", () => {
    const none: WalkEntry[] = [];
    expect(scannableCorpus(true, ok(none, "k2", 1), { entries: A, key: "k1" })).toEqual({
      entries: none,
      key: "k2",
      stale: false,
    });
  });

  it("scans the held corpus, marked stale, while a refetch is in flight", () => {
    // The bug this exists for: the fetch effect publishes an EMPTY streaming
    // state before the request resolves, so a keystroke during a refetch used
    // to rank against nothing and paint a blank list.
    expect(scannableCorpus(true, streaming([], "k2", 1), { entries: A, key: "k1" })).toEqual({
      entries: A,
      key: "k1",
      stale: true,
    });
  });

  it("scans the held corpus while the walk is invalidated to idle", () => {
    expect(scannableCorpus(true, { status: "idle" }, { entries: A, key: "k1" })).toEqual({
      entries: A,
      key: "k1",
      stale: true,
    });
  });

  it("prefers the fresh corpus the moment its first batch lands", () => {
    expect(scannableCorpus(true, streaming(B, "k2", 1), { entries: A, key: "k1" })).toEqual({
      entries: B,
      key: "k2",
      stale: false,
    });
  });

  it("scans nothing when the walk errored — the error is the answer", () => {
    expect(
      scannableCorpus(true, { status: "error", message: "nope", key: "k2", forRefresh: 1 }, {
        entries: A,
        key: "k1",
      }).entries,
    ).toBeNull();
  });

  it("scans nothing when nothing has ever been in hand", () => {
    expect(scannableCorpus(true, { status: "idle" }, null).entries).toBeNull();
  });
});
