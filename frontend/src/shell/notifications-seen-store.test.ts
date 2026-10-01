// bun's test runtime has no localStorage, so stand one up — same shape
// `dismiss-store.test.ts` uses: a real (tiny) store rather than a spy, since
// what matters is the round trip through a JSON-encoded string key.
import { beforeEach, expect, test } from "bun:test";
import {
  _resetSeenStoreForTest,
  getFirstSeenAt,
  isSeen,
  markSeen,
  stampFirstSeen,
} from "@shell/notifications-seen-store";

const store = new Map<string, string>();
(globalThis as { localStorage?: unknown }).localStorage = {
  getItem: (k: string) => (store.has(k) ? store.get(k)! : null),
  setItem: (k: string, v: string) => void store.set(k, String(v)),
  removeItem: (k: string) => void store.delete(k),
  clear: () => store.clear(),
};

beforeEach(() => {
  store.clear();
  _resetSeenStoreForTest();
});

test("a key never synced before is unseen and has no first-seen stamp", () => {
  const state = stampFirstSeen(["a"], 1000);
  expect(isSeen(state, "a")).toBe(false);
  expect(getFirstSeenAt(state, "a", 999)).toBe(1000);
});

test("markSeen persists across a later stampFirstSeen call", () => {
  stampFirstSeen(["a", "b"], 1000);
  markSeen(["a"]);
  const state = stampFirstSeen(["a", "b"], 2000);
  expect(isSeen(state, "a")).toBe(true);
  expect(isSeen(state, "b")).toBe(false);
});

test("firstSeenAt is stamped once and never moves on a later sync", () => {
  stampFirstSeen(["a"], 1000);
  const state = stampFirstSeen(["a"], 5000);
  expect(getFirstSeenAt(state, "a", 0)).toBe(1000);
});

test("a key missing from a later, partial presentKeys call is NOT forgotten (R4 fix)", () => {
  // A render with fewer rows than last time — a filtered view, a transient
  // empty list, the panel's very first paint — must not be read as "every
  // other row is gone". Pruning is age/size-based, never presence-based.
  stampFirstSeen(["a", "b"], 1000);
  markSeen(["a", "b"]);
  const state = stampFirstSeen(["b"], 2000);
  expect(isSeen(state, "a")).toBe(true);
  expect(isSeen(state, "b")).toBe(true);
  expect(getFirstSeenAt(state, "a", 9999)).toBe(1000);
});

test("an entry older than 30 days is pruned on the next write", () => {
  const THIRTY_DAYS_MS = 30 * 24 * 60 * 60 * 1000;
  stampFirstSeen(["a"], 1000);
  markSeen(["a"]);
  // A write that happens well past the age window drops "a" even though it
  // is still present — pruning only ever happens on write, and only by age
  // or size, never by a render's present-key set.
  const state = stampFirstSeen(["a", "b"], 1000 + THIRTY_DAYS_MS + 1);
  expect(isSeen(state, "a")).toBe(false);
  expect(getFirstSeenAt(state, "a", 4242)).toBe(4242);
  expect(getFirstSeenAt(state, "b", 0)).toBe(1000 + THIRTY_DAYS_MS + 1);
});

test("over the entry cap, the oldest-by-firstSeenAt entries are dropped first", () => {
  // Fill past the 500-entry cap with distinct, ascending firstSeenAt stamps.
  const keys = Array.from({ length: 501 }, (_, i) => `k${i}`);
  for (const [i, key] of keys.entries()) {
    stampFirstSeen([key], 1000 + i);
  }
  const state = stampFirstSeen(["final"], 1000 + 501);
  // The very first key stamped (oldest) is gone; the most recent 500 remain.
  expect(getFirstSeenAt(state, "k0", -1)).toBe(-1);
  expect(getFirstSeenAt(state, "k500", -1)).toBe(1000 + 500);
  expect(getFirstSeenAt(state, "final", -1)).toBe(1000 + 501);
});

test("a key's reappearance under a changed signature is unseen again (R4)", () => {
  // The seen store itself does not know about "signatures" — a caller folds
  // a changed repo signature into the key (e.g. `repo:${root}@${behind}`),
  // so the OLD key simply never gets marked seen again under its new name,
  // and the NEW key starts fresh/unseen on its own.
  stampFirstSeen(["repo:/x@3"], 1000);
  markSeen(["repo:/x@3"]);
  const state = stampFirstSeen(["repo:/x@5"], 2000);
  expect(isSeen(state, "repo:/x@5")).toBe(false);
  expect(isSeen(state, "repo:/x@3")).toBe(true);
});

test("message keys never touch localStorage — they live in memory only", () => {
  stampFirstSeen(["message:1"], 1000);
  markSeen(["message:1"]);
  // Nothing was ever written to the backing store for a message key.
  expect(store.has("fused-render:notifications-seen")).toBe(false);
  const state = stampFirstSeen(["message:1"], 2000);
  expect(isSeen(state, "message:1")).toBe(true);
  // A fresh module instance (simulated here by a full reset) forgets it —
  // the same as a reload or a second window would.
  _resetSeenStoreForTest();
  const reloaded = stampFirstSeen(["message:1"], 3000);
  expect(isSeen(reloaded, "message:1")).toBe(false);
});

test("malformed JSON is treated as empty state, not a throw", () => {
  store.set("fused-render:notifications-seen", "{oops");
  _resetSeenStoreForTest();
  expect(() => stampFirstSeen(["a"], 1000)).not.toThrow();
  const state = stampFirstSeen(["a"], 1000);
  expect(isSeen(state, "a")).toBe(false);
});

test("a write that fails to persist never throws, and still updates the live state", () => {
  const real = (globalThis as { localStorage?: unknown }).localStorage;
  (globalThis as { localStorage?: unknown }).localStorage = {
    getItem: () => {
      throw new Error("denied");
    },
    setItem: () => {
      throw new Error("denied");
    },
  };
  try {
    // A failed disk write degrades silently — the in-memory cache this
    // module keeps still reflects every call, so the current session reads
    // correctly even though none of it survives a reload.
    expect(() => stampFirstSeen(["a"], 1000)).not.toThrow();
    expect(() => markSeen(["a"])).not.toThrow();
    const state = stampFirstSeen(["a"], 1000);
    expect(isSeen(state, "a")).toBe(true);
  } finally {
    (globalThis as { localStorage?: unknown }).localStorage = real;
  }
});

test("a reload after storage denies every write starts over from nothing", () => {
  const real = (globalThis as { localStorage?: unknown }).localStorage;
  (globalThis as { localStorage?: unknown }).localStorage = {
    getItem: () => {
      throw new Error("denied");
    },
    setItem: () => {
      throw new Error("denied");
    },
  };
  try {
    stampFirstSeen(["a"], 1000);
    markSeen(["a"]);
    // `_resetSeenStoreForTest` simulates a fresh module load, which re-reads
    // from (still-denying) storage — nothing was ever persisted, so it comes
    // back empty rather than throwing.
    expect(() => _resetSeenStoreForTest()).not.toThrow();
    const state = stampFirstSeen(["a"], 2000);
    expect(isSeen(state, "a")).toBe(false);
  } finally {
    (globalThis as { localStorage?: unknown }).localStorage = real;
  }
});
