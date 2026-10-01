// Persistence for the Notifications panel's seen/unseen state (status-
// popovers R4) and the client-side "first seen" clock rows with no server
// timestamp use for newest-first sorting (R5) — one localStorage entry,
// `dismiss-store.ts`'s own defensive pattern (a private window, a full quota
// or malformed JSON all just behave as "nothing recorded" rather than
// throwing).
//
// A row counts as unseen until the Notifications panel has been open while
// it was present (R4) — `RepoUpdatesDock.tsx` calls `markSeen` with the keys
// on screen when the panel closes (or ~1.5s after it opens), never on open
// itself, so the user can still see what's new during the very open in which
// they're looking at it.
//
// `firstSeenAt` is stamped the first time a key is ever synced here and never
// updated again, so it tracks "when did this row first exist" — the clock R5
// sorts repo rows, pairings and waiting tasks by (messages/jobs have their
// own server-backed `updatedAt`/`finished_at` instead and never touch this).
//
// Pruned to exactly the keys currently present on every `syncPresentKeys`
// call, so a repo/task/message that scrolled out of existence long ago does
// not sit in storage forever — same shape as `dismiss-store.ts`'s own
// per-session pruning rule, just carrying two maps instead of one.
const STORAGE_KEY = "fused-render:notifications-seen";

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

/** Call once per set of rows currently on screen (every render of the
 *  Notifications panel's contents is fine — it is cheap and idempotent).
 *  Prunes `seen`/`firstSeenAt` down to exactly `presentKeys`, stamping a
 *  fresh `firstSeenAt` for any key seen for the first time, and returns the
 *  resulting state for `isSeen`/`firstSeenAt` to read. A key's reappearance
 *  under a changed signature (R4: "a key change makes it unseen again") is
 *  handled by the CALLER computing a new key for it — this function only
 *  ever prunes and stamps, it never decides what a key IS. */
export function syncPresentKeys(presentKeys: readonly string[], now: number = Date.now()): SeenState {
  const state = loadRaw();
  const presentSet = new Set(presentKeys);
  const seen = state.seen.filter((k) => presentSet.has(k));
  const firstSeenAt: Record<string, number> = {};
  for (const key of presentKeys) {
    firstSeenAt[key] = state.firstSeenAt[key] ?? now;
  }
  const next: SeenState = { seen, firstSeenAt };
  saveRaw(next);
  return next;
}

export function isSeen(state: SeenState, key: string): boolean {
  return state.seen.includes(key);
}

/** `fallback` covers a key this store has never stamped yet (the render that
 *  first introduces it, before its own `syncPresentKeys` call has landed) —
 *  callers pass `Date.now()` so a brand new row sorts as "now" rather than
 *  as epoch zero. */
export function getFirstSeenAt(state: SeenState, key: string, fallback: number): number {
  return state.firstSeenAt[key] ?? fallback;
}

/** Mark every one of `keys` seen — called on panel close, or ~1.5s after it
 *  opens (R4), never on open itself. */
export function markSeen(keys: readonly string[]): void {
  const state = loadRaw();
  const seenSet = new Set(state.seen);
  for (const k of keys) seenSet.add(k);
  saveRaw({ ...state, seen: [...seenSet] });
}

/** Test-only reset so suites don't leak state into one another the way
 *  `dismiss-store.ts`'s own callers avoid by resetting their module-level
 *  caches between tests. Not used by any non-test caller. */
export function _resetSeenStoreForTest(): void {
  saveRaw(EMPTY);
}
