// bun's test runtime has no localStorage, so stand one up — same shape
// `dismiss-store.test.ts` uses: a real (tiny) store rather than a spy, since
// what matters is the round trip through a JSON-encoded string key.
import { beforeEach, expect, test } from "bun:test";
import {
  _resetSeenStoreForTest,
  getFirstSeenAt,
  isSeen,
  markSeen,
  syncPresentKeys,
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
  const state = syncPresentKeys(["a"], 1000);
  expect(isSeen(state, "a")).toBe(false);
  expect(getFirstSeenAt(state, "a", 999)).toBe(1000);
});

test("markSeen persists across a later syncPresentKeys call", () => {
  syncPresentKeys(["a", "b"], 1000);
  markSeen(["a"]);
  const state = syncPresentKeys(["a", "b"], 2000);
  expect(isSeen(state, "a")).toBe(true);
  expect(isSeen(state, "b")).toBe(false);
});

test("firstSeenAt is stamped once and never moves on a later sync", () => {
  syncPresentKeys(["a"], 1000);
  const state = syncPresentKeys(["a"], 5000);
  expect(getFirstSeenAt(state, "a", 0)).toBe(1000);
});

test("pruning: a key that drops out of presentKeys is forgotten, seen state and first-seen alike", () => {
  syncPresentKeys(["a", "b"], 1000);
  markSeen(["a", "b"]);
  // "a" scrolls out of existence (dismissed, resolved, evicted — whatever the
  // caller's reason); only "b" is still present.
  const state = syncPresentKeys(["b"], 2000);
  expect(isSeen(state, "b")).toBe(true);
  // "a" is gone entirely, not just unseen — a later reappearance under the
  // SAME key would otherwise wrongly read as "already seen" from stale data.
  expect(isSeen(state, "a")).toBe(false);
  expect(getFirstSeenAt(state, "a", 9999)).toBe(9999);
});

test("a key's reappearance under a changed signature is unseen again (R4)", () => {
  // The seen store itself does not know about "signatures" — it only ever
  // prunes/stamps whatever keys the caller currently passes it. A caller
  // folding a changed repo signature into the key (e.g. `repo:${root}@${behind}`)
  // gets "unseen again on change" for free: the OLD key simply stops being
  // present and is pruned away, and the NEW key starts fresh.
  syncPresentKeys(["repo:/x@3"], 1000);
  markSeen(["repo:/x@3"]);
  const state = syncPresentKeys(["repo:/x@5"], 2000);
  expect(isSeen(state, "repo:/x@5")).toBe(false);
  expect(isSeen(state, "repo:/x@3")).toBe(false);
});

test("malformed JSON is treated as empty state, not a throw", () => {
  store.set("fused-render:notifications-seen", "{oops");
  expect(() => syncPresentKeys(["a"], 1000)).not.toThrow();
  const state = syncPresentKeys(["a"], 1000);
  expect(isSeen(state, "a")).toBe(false);
});

test("unavailable storage degrades to nothing seen, and never throws", () => {
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
    expect(() => syncPresentKeys(["a"], 1000)).not.toThrow();
    expect(() => markSeen(["a"])).not.toThrow();
    const state = syncPresentKeys(["a"], 1000);
    expect(isSeen(state, "a")).toBe(false);
  } finally {
    (globalThis as { localStorage?: unknown }).localStorage = real;
  }
});
