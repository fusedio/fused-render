// The Notifications panel's state is server-backed since D1386
// (`/api/notifications/state`). `fetch` is stubbed per file — never
// `mock.module("@platform/lib/api")`, which replaces the specifier for the
// WHOLE `bun test` process (RepoUpdatesDock.test.tsx's header) — and records
// every request so the merge patches the store sends can be asserted. bun has
// no localStorage either, so a tiny real one stands up for the one-shot import.
import { afterAll, beforeEach, expect, test } from "bun:test";
import {
  _currentSnapshotForTest,
  _resetSeenStoreForTest,
  dismissAttention,
  dismissRepo,
  dismissRepoMany,
  getAttentionDismissed,
  getFirstSeenAt,
  getRepoDismissed,
  hydrateNotificationsState,
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

type Sent = { method: string; url: string; body: unknown };
let sent: Sent[] = [];
let serverRecord: unknown = { seen: [], firstSeenAt: {}, repoDismissed: {}, attentionDismissed: {} };
let postFails = false;
const realFetch = globalThis.fetch;
globalThis.fetch = (async (url: string, init?: RequestInit) => {
  const method = init?.method ?? "GET";
  const body = init?.body ? JSON.parse(String(init.body)) : undefined;
  sent.push({ method, url, body });
  if (method !== "GET" && postFails) {
    return { ok: false, status: 500, json: async () => ({ detail: "boom" }) } as unknown as Response;
  }
  return { ok: true, status: 200, json: async () => serverRecord } as unknown as Response;
}) as unknown as typeof fetch;
afterAll(() => {
  globalThis.fetch = realFetch;
});

const posts = () => sent.filter((r) => r.method === "POST").map((r) => r.body);

beforeEach(() => {
  store.clear();
  sent = [];
  postFails = false;
  serverRecord = { seen: [], firstSeenAt: {}, repoDismissed: {}, attentionDismissed: {} };
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

test("past the cap, a render's own rows are never pruned, so a re-render is a no-op (D1386)", () => {
  // Pruning a key that is ON SCREEN made the next render re-stamp it, prune
  // another, emit, and re-run — a loop. The cap only trims rows that are gone.
  const old = Array.from({ length: 10 }, (_, i) => `gone${i}`);
  stampFirstSeen(old, 500);
  const present = Array.from({ length: 501 }, (_, i) => `k${i}`);
  const first = stampFirstSeen(present, 1000);
  for (const k of present) expect(getFirstSeenAt(first, k, -1)).toBe(1000);
  expect(getFirstSeenAt(first, "gone0", -1)).toBe(-1);
  // The identical render changes nothing and hands back the same object.
  expect(stampFirstSeen(present, 2000)).toBe(first);
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

test("message keys never leave the window — they live in memory only", () => {
  stampFirstSeen(["message:1"], 1000);
  markSeen(["message:1"]);
  // Nothing was ever sent for a message key (the server rejects them).
  expect(posts()).toEqual([]);
  const state = stampFirstSeen(["message:1"], 2000);
  expect(isSeen(state, "message:1")).toBe(true);
  // A fresh module instance (simulated here by a full reset) forgets it —
  // the same as a reload or a second window would.
  _resetSeenStoreForTest();
  const reloaded = stampFirstSeen(["message:1"], 3000);
  expect(isSeen(reloaded, "message:1")).toBe(false);
});

test("markSeen posts the new keys at once, carrying any stamps still waiting", () => {
  stampFirstSeen(["a", "message:2"], 1000);
  // The stamp is coalesced: nothing has gone out yet.
  expect(posts()).toEqual([]);
  markSeen(["a", "message:2"]);
  expect(posts()).toEqual([{ seen: ["a"], firstSeenAt: { a: 1000 } }]);
  // Re-marking changes nothing and sends nothing.
  markSeen(["a"]);
  expect(posts().length).toBe(1);
});

test("stamps from consecutive renders go out as ONE coalesced merge", async () => {
  stampFirstSeen(["a"], 1000);
  stampFirstSeen(["a", "b"], 1001);
  expect(posts()).toEqual([]);
  await new Promise((r) => setTimeout(r, 600));
  expect(posts()).toEqual([{ firstSeenAt: { a: 1000, b: 1001 } }]);
});

test("a dismissal updates the cache, re-snapshots, and posts only its own id", () => {
  const before = _currentSnapshotForTest();
  dismissRepo("/r/one", "main@3");
  expect(getRepoDismissed()).toEqual({ "/r/one": "main@3" });
  expect(_currentSnapshotForTest()).not.toBe(before);
  dismissRepoMany({ "/r/one": "main@3", "/r/two": "main@1" });
  dismissAttention("task:x", "Q?");
  expect(getAttentionDismissed()).toEqual({ "task:x": "Q?" });
  // The unchanged id is not re-sent.
  expect(posts()).toEqual([
    { repoDismissed: { "/r/one": "main@3" } },
    { repoDismissed: { "/r/two": "main@1" } },
    { attentionDismissed: { "task:x": "Q?" } },
  ]);
  // Same signature again: no write, same object (a stable render input).
  const held = getRepoDismissed();
  dismissRepo("/r/one", "main@3");
  expect(getRepoDismissed()).toBe(held);
  expect(posts().length).toBe(3);
});

test("a failed POST never throws, and the cache still holds the change", async () => {
  postFails = true;
  expect(() => markSeen(["a"])).not.toThrow();
  expect(() => dismissRepo("/r", "s")).not.toThrow();
  await new Promise((r) => setTimeout(r, 0));
  expect(isSeen(_currentSnapshotForTest(), "a")).toBe(true);
  expect(getRepoDismissed()).toEqual({ "/r": "s" });
});

test("hydration reads the server record once, under what this window already wrote", async () => {
  serverRecord = {
    seen: ["s1"],
    firstSeenAt: { s1: 10, early: 50 },
    repoDismissed: { "/r": "old" },
    attentionDismissed: {},
  };
  // Written before the GET answered: newer than the server's copy.
  stampFirstSeen(["early"], 40);
  dismissRepo("/r", "new");
  await hydrateNotificationsState();
  await hydrateNotificationsState();
  expect(sent.filter((r) => r.method === "GET").length).toBe(1);
  const state = _currentSnapshotForTest();
  expect(isSeen(state, "s1")).toBe(true);
  expect(getFirstSeenAt(state, "early", 0)).toBe(40); // the earlier stamp wins
  expect(getRepoDismissed()).toEqual({ "/r": "new" });
});

test("the one-shot import pushes the three old keys in ONE merge, then removes them", async () => {
  store.set("fused-render:notifications-seen", JSON.stringify({
    seen: ["a", "message:9"],
    firstSeenAt: { a: 100, "message:9": 1 },
  }));
  store.set("fused-render:repo-updates-dismissed", JSON.stringify({ "/r": "x", "/held": "old" }));
  store.set("fused-render:attention-dismissed", "{oops");
  serverRecord = { seen: [], firstSeenAt: {}, repoDismissed: { "/held": "server" }, attentionDismissed: {} };
  await hydrateNotificationsState();
  // Message keys are dropped (the server would 400 the whole merge), malformed
  // JSON imports as nothing, and an id the server already holds is not
  // overwritten by the older row.
  expect(posts()).toEqual([
    { seen: ["a"], firstSeenAt: { a: 100 }, repoDismissed: { "/r": "x" }, attentionDismissed: {} },
  ]);
  expect(store.size).toBe(0);
  expect(isSeen(_currentSnapshotForTest(), "a")).toBe(true);
  expect(getRepoDismissed()).toEqual({ "/held": "server", "/r": "x" });
});

test("a failed import POST keeps the old keys so the next load retries", async () => {
  store.set("fused-render:repo-updates-dismissed", JSON.stringify({ "/r": "x" }));
  postFails = true;
  await hydrateNotificationsState();
  expect(store.has("fused-render:repo-updates-dismissed")).toBe(true);
  // The session still shows the import.
  expect(getRepoDismissed()).toEqual({ "/r": "x" });
});

test("_resetSeenStoreForTest seeds a hydrated record", () => {
  _resetSeenStoreForTest({ seen: ["k"], firstSeenAt: { k: 5 }, repoDismissed: { "/r": "s" } });
  expect(isSeen(_currentSnapshotForTest(), "k")).toBe(true);
  expect(getRepoDismissed()).toEqual({ "/r": "s" });
});

test("marking a message key seen yields a fresh snapshot object (chip count must refresh)", () => {
  // `useSeenSnapshot` is a `useSyncExternalStore` read: React skips the
  // re-render a listener would otherwise trigger whenever `getSnapshot()`
  // returns the SAME object it returned last time. A message-only
  // `markSeen` call changes real seen state (`isSeen` flips from false to
  // true) but must still hand back a NEW object, or a subscriber that isn't
  // re-rendering for any other reason (e.g. the chip after a panel close
  // with no other prop change) never finds out.
  const before = _currentSnapshotForTest();
  markSeen(["message:1"]);
  const after = _currentSnapshotForTest();
  expect(isSeen(after, "message:1")).toBe(true);
  expect(after).not.toBe(before);
});

test("marking an already-seen key produces no new snapshot object (getSnapshot must stay stable)", () => {
  markSeen(["a", "message:1"]);
  const stable = _currentSnapshotForTest();
  // Re-marking the exact same keys changes nothing — `getSnapshot` must
  // return the IDENTICAL reference, or every subscriber re-renders forever
  // on a no-op call.
  markSeen(["a", "message:1"]);
  expect(_currentSnapshotForTest()).toBe(stable);
});
