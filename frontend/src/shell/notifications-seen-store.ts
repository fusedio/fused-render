// Persistence for the Notifications panel's seen/unseen state (status-
// popovers R4) and the client-side "first seen" clock rows with no server
// timestamp use for newest-first sorting (R5) — one localStorage entry,
// `dismiss-store.ts`'s own defensive pattern (a private window, a full quota
// or malformed JSON all just behave as "nothing recorded" rather than
// throwing).
//
// A row counts as unseen until the Notifications panel has been open while
// it was present (R4) — `RepoUpdatesDock.tsx` calls `markSeen` with every key
// that was present at any point during an open, on close.
//
// `firstSeenAt` is stamped the first time a key is ever synced here and never
// updated again, so it tracks "when did this row first exist" — the clock R5
// sorts repo rows, pairings and waiting tasks by (messages/jobs have their
// own server-backed `updatedAt`/`finished_at` instead and never touch this).
//
// Persisted state is pruned on every write, never on read: an entry drops out
// once it is older than `MAX_AGE_MS`, and once there are more than
// `MAX_ENTRIES` the oldest-by-`firstSeenAt` are dropped first. Pruning is
// never driven by whether a key is present in a given render — a render with
// an empty or partial row list (the panel's very first paint, a filtered
// view, a transient empty state) must not be read as "everything else is
// gone", or reload would silently forget every row's seen state.
//
// A client message's id (`notify()`'s row id) is NOT stable across a reload
// or a second window — two different windows can mint the same id for two
// different messages, or the same message can get a new id next time it's
// raised. Message keys (prefixed `message:`) therefore never touch
// localStorage at all: they live only in `messageSeenSet`, an in-memory
// module-level set that starts empty every time this module loads.
import { useSyncExternalStore } from "react";

const STORAGE_KEY = "fused-render:notifications-seen";

const MAX_AGE_MS = 30 * 24 * 60 * 60 * 1000;
const MAX_ENTRIES = 500;

const MESSAGE_KEY_PREFIX = "message:";
function isMessageKey(key: string): boolean {
  return key.startsWith(MESSAGE_KEY_PREFIX);
}

export interface SeenState {
  seen: string[];
  firstSeenAt: Record<string, number>;
}

const EMPTY: SeenState = { seen: [], firstSeenAt: {} };

function loadRaw(): SeenState {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return EMPTY;
    const parsed = JSON.parse(raw) as unknown;
    if (!parsed || typeof parsed !== "object") return EMPTY;
    const obj = parsed as Record<string, unknown>;
    const seen = Array.isArray(obj.seen)
      ? obj.seen.filter((x): x is string => typeof x === "string")
      : [];
    const firstSeenAt: Record<string, number> = {};
    if (obj.firstSeenAt && typeof obj.firstSeenAt === "object") {
      for (const [k, v] of Object.entries(obj.firstSeenAt as Record<string, unknown>)) {
        if (typeof v === "number") firstSeenAt[k] = v;
      }
    }
    return { seen, firstSeenAt };
  } catch {
    return EMPTY;
  }
}

function saveRaw(state: SeenState): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
  } catch {
    // storage unavailable — seen state is best-effort, so a failed write is fine
  }
}

/** Drops entries older than `MAX_AGE_MS`, then — if still over `MAX_ENTRIES`
 *  — drops the oldest-by-`firstSeenAt` until the cap holds. `seen` is
 *  filtered down to whatever keys survive in `firstSeenAt`, since every
 *  persisted key is stamped there the moment it is first synced. */
function pruneByAgeAndSize(state: SeenState, now: number): SeenState {
  let entries = Object.entries(state.firstSeenAt).filter(([, ts]) => now - ts <= MAX_AGE_MS);
  if (entries.length > MAX_ENTRIES) {
    entries = entries.sort((a, b) => b[1] - a[1]).slice(0, MAX_ENTRIES);
  }
  const firstSeenAt = Object.fromEntries(entries);
  const keep = new Set(entries.map(([k]) => k));
  const seen = state.seen.filter((k) => keep.has(k));
  return { seen, firstSeenAt };
}

// In-memory only — never persisted, never pruned by age/size (a window's
// lifetime already bounds it). Cleared by `_resetSeenStoreForTest()`.
const messageSeenSet = new Set<string>();

let cachedState: SeenState = loadRaw();
let snapshot: SeenState = cachedState;
const listeners = new Set<() => void>();

function commit(next: SeenState): void {
  cachedState = next;
  saveRaw(next);
}

function emit(): void {
  snapshot = cachedState;
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** Reactive read of the persisted (non-message) seen state — re-renders the
 *  caller whenever `markSeen`/`stampFirstSeen` change it, so a chip count or
 *  a row's section stays live while the panel is open (R4). Message-key seen
 *  state lives outside this snapshot entirely (see `isSeen`), but every
 *  `markSeen` call still triggers the same re-render regardless of which of
 *  the two it touched. */
export function useSeenSnapshot(): SeenState {
  return useSyncExternalStore(subscribe, () => snapshot);
}

/** Call with every row key present on screen (every render of the
 *  Notifications panel's contents is fine — it is cheap and idempotent).
 *  Stamps a fresh `firstSeenAt` for any non-message key seen for the first
 *  time; never removes a key just because it is absent from `presentKeys`
 *  (pruning is age/size-based — see `pruneByAgeAndSize` — and happens here
 *  only as a side effect of writing). Message keys (`message:`-prefixed)
 *  are ignored entirely — they carry their own server timestamp (R5) and
 *  never touch this store. */
export function stampFirstSeen(presentKeys: readonly string[], now: number = Date.now()): SeenState {
  let changed = false;
  const firstSeenAt = { ...cachedState.firstSeenAt };
  for (const key of presentKeys) {
    if (isMessageKey(key)) continue;
    if (firstSeenAt[key] === undefined) {
      firstSeenAt[key] = now;
      changed = true;
    }
  }
  if (!changed) return cachedState;
  const next = pruneByAgeAndSize({ seen: cachedState.seen, firstSeenAt }, now);
  commit(next);
  emit();
  return next;
}

/** A message key (`message:`-prefixed) reads the in-memory-only set;
 *  anything else reads the persisted `state.seen` array. */
export function isSeen(state: SeenState, key: string): boolean {
  if (isMessageKey(key)) return messageSeenSet.has(key);
  return state.seen.includes(key);
}

/** `fallback` covers a key this store has never stamped yet (the render that
 *  first introduces it, before its own `stampFirstSeen` call has landed) —
 *  callers pass `Date.now()` so a brand new row sorts as "now" rather than
 *  as epoch zero. */
export function getFirstSeenAt(state: SeenState, key: string, fallback: number): number {
  return state.firstSeenAt[key] ?? fallback;
}

/** Mark every one of `keys` seen — called on panel close with every key that
 *  was present at any point during that open (R4). A message key goes into
 *  the in-memory set; everything else is merged into persisted `seen`
 *  as-is. Pruning by age/size happens only in `stampFirstSeen` (the regular
 *  per-render write path) — not here, since this is the one call site that
 *  does not carry the caller's own notion of "now", and reusing `Date.now()`
 *  here would prune entries stamped under a test's (or a future caller's)
 *  synthetic clock. */
export function markSeen(keys: readonly string[]): void {
  let changed = false;
  const seenSet = new Set(cachedState.seen);
  for (const key of keys) {
    if (isMessageKey(key)) {
      if (!messageSeenSet.has(key)) {
        messageSeenSet.add(key);
        changed = true;
      }
    } else if (!seenSet.has(key)) {
      seenSet.add(key);
      changed = true;
    }
  }
  if (seenSet.size !== cachedState.seen.length) {
    commit({ ...cachedState, seen: [...seenSet] });
  }
  if (changed) emit();
}

/** Test-only reset so suites don't leak state into one another the way
 *  `dismiss-store.ts`'s own callers avoid by resetting their module-level
 *  caches between tests. Not used by any non-test caller.
 *
 *  Also drops every subscriber: a suite that never unmounts its test
 *  renderers (several don't — `RepoUpdatesDock.test.tsx`'s own tests build a
 *  fresh renderer per test and let the old ones linger for the rest of the
 *  run) would otherwise leave a PREVIOUS test's component still subscribed
 *  when this one starts. That component's `useLayoutEffect` fires on
 *  ANY emit — including ones this test's own `markSeen`/`stampFirstSeen`
 *  calls raise — and would re-stamp ITS OWN stale keys into the store this
 *  test is now reading, corrupting it. Clearing `listeners` here severs
 *  every such stale subscription before the next test's components
 *  (re-)subscribe on their own first render. */
export function _resetSeenStoreForTest(): void {
  messageSeenSet.clear();
  cachedState = loadRaw();
  listeners.clear();
  emit();
}
