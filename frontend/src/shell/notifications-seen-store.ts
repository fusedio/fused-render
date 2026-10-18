// The Notifications panel's own state, server-backed since D1386
// (`GET/POST /api/notifications/state`, fused_render/shell/notifications.py):
//
//   * `seen` / `firstSeenAt` — the seen/unseen split (status-popovers R4) and
//     the client-side "first seen" clock rows with no server timestamp use for
//     newest-first sorting (R5);
//   * `repoDismissed` / `attentionDismissed` — a repo row's ✕ and a
//     waiting-task row's ✕, each `{ id: signature }`, so a dismissal expires
//     the moment its row's signature moves (RepoUpdatesDock.tsx).
//
// WHY A SERVER AND NOT localStorage: localStorage is per ORIGIN, and the
// desktop app's origin is not fixed — it walks ports 1777..1787 and then an
// ephemeral one, and each port is a new origin with empty storage. A relaunch
// that landed on another port brought every dismissed row back and every seen
// row back to unseen. The rule (D1386): anything the user would miss across a
// relaunch lives behind an API.
//
// THE bookmarks.ts SHAPE: reads are synchronous off an in-memory cache (React
// renders cannot await); `hydrateNotificationsState()` fills it ONCE per
// document at boot; every write updates the cache, re-renders subscribers,
// then POSTs a MERGE patch fire-and-forget. A POST's answer is never adopted:
// a merge landing while this window has newer stamps or dismissals in hand
// would clobber them, and the server merge is monotone anyway (seen unions,
// the earlier first-seen stamp wins, a dismissal is the posted signature), so
// what this window sent is already what the server holds for those ids. A
// failed POST costs that one write across a relaunch, never the session.
//
// A row counts as unseen until the Notifications panel has been open while
// it was present (R4) — `RepoUpdatesDock.tsx` calls `markSeen` with every key
// that was present at any point during an open, on close.
//
// `firstSeenAt` is stamped the first time a key is ever synced here and never
// updated again, so it tracks "when did this row first exist" — the clock R5
// sorts repo rows, pairings and waiting tasks by (messages/jobs have their
// own server-backed `updatedAt`/`finished_at` instead and never touch this).
// `stampFirstSeen` runs on every render of the panel, so its POSTs are
// coalesced (`STAMP_FLUSH_MS`); `markSeen` and the dismissals post at once.
//
// The cache is pruned on every stamp, never on read: an entry drops out once
// it is older than `MAX_AGE_MS`, and once there are more than `MAX_ENTRIES` the
// oldest-by-`firstSeenAt` are dropped first (the server applies the same prune
// to the file on every write). Pruning is never driven by whether a key is
// present in a given render — a render with an empty or partial row list (the
// panel's very first paint, a filtered view, a transient empty state) must not
// be read as "everything else is gone", or reload would silently forget every
// row's seen state.
//
// A client message's id (`notify()`'s row id) is NOT stable across a reload
// or a second window — two different windows can mint the same id for two
// different messages, or the same message can get a new id next time it's
// raised. Message keys (prefixed `message:`) therefore never leave this
// window: they live only in `messageSeenSet`, an in-memory module-level set
// that starts empty every time this module loads (and the server rejects them).
import { useSyncExternalStore } from "react";
import {
  getNotificationsState,
  mergeNotificationsState,
  type NotificationsState,
} from "@platform/lib/api";

const MAX_AGE_MS = 30 * 24 * 60 * 60 * 1000;
const MAX_ENTRIES = 500;
/** How long `stampFirstSeen` waits to batch the stamps of consecutive renders
 *  into one POST. */
const STAMP_FLUSH_MS = 500;

const MESSAGE_KEY_PREFIX = "message:";
function isMessageKey(key: string): boolean {
  return key.startsWith(MESSAGE_KEY_PREFIX);
}

export interface SeenState {
  seen: string[];
  firstSeenAt: Record<string, number>;
}

const EMPTY: NotificationsState = {
  seen: [],
  firstSeenAt: {},
  repoDismissed: {},
  attentionDismissed: {},
};

/** Whatever arrived (the server's record, an old localStorage row) narrowed to
 *  the shape this module trusts: wrong-typed entries are dropped one by one
 *  rather than costing the whole record, and message keys never get in. */
function normalize(raw: unknown): NotificationsState {
  if (!raw || typeof raw !== "object") return EMPTY;
  const obj = raw as Record<string, unknown>;
  const seen = Array.isArray(obj.seen)
    ? obj.seen.filter((x): x is string => typeof x === "string" && !!x && !isMessageKey(x))
    : [];
  const firstSeenAt: Record<string, number> = {};
  if (obj.firstSeenAt && typeof obj.firstSeenAt === "object") {
    for (const [k, v] of Object.entries(obj.firstSeenAt as Record<string, unknown>)) {
      if (typeof v === "number" && Number.isFinite(v) && !isMessageKey(k)) firstSeenAt[k] = v;
    }
  }
  return {
    seen,
    firstSeenAt,
    repoDismissed: stringMap(obj.repoDismissed),
    attentionDismissed: stringMap(obj.attentionDismissed),
  };
}

function stringMap(raw: unknown): Record<string, string> {
  const out: Record<string, string> = {};
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return out;
  for (const [k, v] of Object.entries(raw as Record<string, unknown>)) {
    if (typeof v === "string" && v) out[k] = v;
  }
  return out;
}

/** The server's merge rule, applied locally: `seen` unions, the EARLIER
 *  first-seen stamp wins, and `over`'s dismissals win per id. */
function merge(base: NotificationsState, over: NotificationsState): NotificationsState {
  const firstSeenAt = { ...base.firstSeenAt };
  for (const [k, ts] of Object.entries(over.firstSeenAt)) {
    const had = firstSeenAt[k];
    if (had === undefined || ts < had) firstSeenAt[k] = ts;
  }
  return {
    seen: [...new Set([...base.seen, ...over.seen])],
    firstSeenAt,
    repoDismissed: { ...base.repoDismissed, ...over.repoDismissed },
    attentionDismissed: { ...base.attentionDismissed, ...over.attentionDismissed },
  };
}

/** Drops entries older than `MAX_AGE_MS`, then — if still over `MAX_ENTRIES`
 *  — drops the oldest-by-`firstSeenAt` ABSENT keys until the cap holds. `seen`
 *  is filtered down to whatever keys survive in `firstSeenAt`, since every
 *  key is stamped there the moment it is first synced.
 *
 *  A key in `present` (the render's own rows) is never dropped by the cap,
 *  even past it (D1386 review). Dropping one made the render NOT idempotent:
 *  with more than `MAX_ENTRIES` rows on screen the next render found the
 *  pruned keys unstamped, stamped them again, pruned others, emitted, and the
 *  panel's layout effect re-ran it — a loop. The cap bounds what is
 *  remembered about rows that are GONE; rows on screen are bounded by the
 *  screen. */
function pruneByAgeAndSize(
  state: NotificationsState,
  now: number,
  present: ReadonlySet<string> = new Set(),
): NotificationsState {
  let entries = Object.entries(state.firstSeenAt).filter(([, ts]) => now - ts <= MAX_AGE_MS);
  if (entries.length > MAX_ENTRIES) {
    const onScreen = entries.filter(([k]) => present.has(k));
    const room = Math.max(0, MAX_ENTRIES - onScreen.length);
    const gone = entries
      .filter(([k]) => !present.has(k))
      .sort((a, b) => b[1] - a[1])
      .slice(0, room);
    entries = [...onScreen, ...gone];
  }
  const firstSeenAt = Object.fromEntries(entries);
  const keep = new Set(entries.map(([k]) => k));
  const seen = state.seen.filter((k) => keep.has(k));
  return { ...state, seen, firstSeenAt };
}

// In-memory only — never persisted, never pruned by age/size (a window's
// lifetime already bounds it). Cleared by `_resetSeenStoreForTest()`.
const messageSeenSet = new Set<string>();

let cachedState: NotificationsState = EMPTY;
let snapshot: NotificationsState = cachedState;
const listeners = new Set<() => void>();

function emit(): void {
  snapshot = cachedState;
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** Fire-and-forget: the cache already holds the change (see the header). */
function post(patch: Partial<NotificationsState>): void {
  try {
    mergeNotificationsState(patch).catch(() => {});
  } catch {
    // No fetch at all (a test runtime): the cache still has it.
  }
}

// Stamps waiting for the coalesced POST.
let pendingStamps: Record<string, number> = {};
let stampTimer: ReturnType<typeof setTimeout> | null = null;

function flushStamps(): void {
  if (stampTimer !== null) clearTimeout(stampTimer);
  stampTimer = null;
  if (!Object.keys(pendingStamps).length) return;
  const firstSeenAt = pendingStamps;
  pendingStamps = {};
  post({ firstSeenAt });
}

// ---- hydration + the one-shot import (D1386) -------------------------------

const LEGACY_SEEN_KEY = "fused-render:notifications-seen";
const LEGACY_REPO_DISMISSED_KEY = "fused-render:repo-updates-dismissed";
const LEGACY_ATTENTION_DISMISSED_KEY = "fused-render:attention-dismissed";
const LEGACY_KEYS = [LEGACY_SEEN_KEY, LEGACY_REPO_DISMISSED_KEY, LEGACY_ATTENTION_DISMISSED_KEY];

function readLegacyJson(key: string): unknown {
  try {
    const raw = localStorage.getItem(key);
    return raw ? JSON.parse(raw) : null;
  } catch {
    // Malformed or unreadable: nothing to import from this key.
    return null;
  }
}

function anyLegacyKey(): boolean {
  try {
    return LEGACY_KEYS.some((k) => localStorage.getItem(k) !== null);
  } catch {
    return false;
  }
}

let hydration: Promise<void> | null = null;
/** Bumped by the test reset so a hydration still in flight from an earlier
 *  test cannot land in the next one's store. */
let epoch = 0;

/** Fill the cache from the server ONCE per document (main.tsx's boot
 *  hydration calls this), then run the one-shot import of the three old
 *  localStorage keys: ONE merge POST carrying all three, then the keys are
 *  removed. No server-side "already imported" marker — a second window racing
 *  this one re-pushes the same values, harmless under the merge. A failed POST
 *  leaves the keys so the next load retries. Never rejects. */
export function hydrateNotificationsState(): Promise<void> {
  if (!hydration) hydration = runHydration();
  return hydration;
}

async function runHydration(): Promise<void> {
  const mine = epoch;
  let server: NotificationsState | null = null;
  try {
    server = normalize(await getNotificationsState());
  } catch {
    // Server unreachable: keep what this window has. A later call retries.
    if (mine === epoch) hydration = null;
  }
  if (mine !== epoch) return;
  // Anything this window wrote before the GET answered is newer than the
  // server's copy, so it is merged OVER it rather than replaced by it.
  if (server) cachedState = merge(server, cachedState);

  if (anyLegacyKey()) {
    const legacySeen = normalize(readLegacyJson(LEGACY_SEEN_KEY));
    // A dismissal the server already holds for an id is at least as new as
    // the old row (another origin migrated first), so only ids it lacks go up.
    const fresh = (old: Record<string, string>, held: Record<string, string>) =>
      Object.fromEntries(Object.entries(old).filter(([id]) => !(id in held)));
    const patch: NotificationsState = {
      seen: legacySeen.seen,
      firstSeenAt: legacySeen.firstSeenAt,
      repoDismissed: fresh(
        stringMap(readLegacyJson(LEGACY_REPO_DISMISSED_KEY)),
        cachedState.repoDismissed,
      ),
      attentionDismissed: fresh(
        stringMap(readLegacyJson(LEGACY_ATTENTION_DISMISSED_KEY)),
        cachedState.attentionDismissed,
      ),
    };
    cachedState = merge(cachedState, patch);
    // Show the merged state NOW, not when the import POST settles: the panel
    // would otherwise sit on the pre-merge cache for the round trip — or for
    // good, if the POST hangs. The POST's result changes nothing on screen
    // (the cache already holds the patch), so it needs no emit of its own.
    emit();
    try {
      await mergeNotificationsState(patch);
      for (const key of LEGACY_KEYS) {
        try {
          localStorage.removeItem(key);
        } catch {
          /* removal failed: the next load re-pushes the same merge, harmless */
        }
      }
    } catch {
      // Keep the keys: the next load retries the import.
    }
    return;
  }
  emit();
}

// ---- reads ---------------------------------------------------------------

/** Reactive read of the server-backed (non-message) state — re-renders the
 *  caller whenever a stamp, a seen mark or a dismissal changes it, so a chip
 *  count, a row's section or a dismissed row stays live while the panel is
 *  open (R4). Message-key seen state lives outside this snapshot entirely (see
 *  `isSeen`), but every `markSeen` call still triggers the same re-render
 *  regardless of which of the two it touched. The snapshot is the whole
 *  record, so a `SeenState` reader and a dismissal reader share one
 *  subscription. */
export function useSeenSnapshot(): NotificationsState {
  return useSyncExternalStore(subscribe, () => snapshot);
}

/** The repo rows' dismissals, `{ root: repoDismissSignature }`. Same object
 *  until a dismissal changes it, so it is safe as a render input. */
export function getRepoDismissed(): Record<string, string> {
  return cachedState.repoDismissed;
}

/** The waiting-task rows' dismissals, `{ key: attentionDismissSignature }`. */
export function getAttentionDismissed(): Record<string, string> {
  return cachedState.attentionDismissed;
}

/** A message key (`message:`-prefixed) reads the in-memory-only set;
 *  anything else reads the server-backed `state.seen` array. */
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

// ---- writes --------------------------------------------------------------

/** Call with every row key present on screen (every render of the
 *  Notifications panel's contents is fine — it is cheap and idempotent, and
 *  the POST is coalesced). Stamps a fresh `firstSeenAt` for any non-message
 *  key seen for the first time; never removes a key just because it is absent
 *  from `presentKeys` (pruning is age/size-based — see `pruneByAgeAndSize` —
 *  and happens here only as a side effect of writing). Message keys
 *  (`message:`-prefixed) are ignored entirely — they carry their own server
 *  timestamp (R5) and never touch this store. */
export function stampFirstSeen(presentKeys: readonly string[], now: number = Date.now()): NotificationsState {
  const fresh: Record<string, number> = {};
  for (const key of presentKeys) {
    if (isMessageKey(key)) continue;
    if (cachedState.firstSeenAt[key] === undefined && fresh[key] === undefined) fresh[key] = now;
  }
  if (!Object.keys(fresh).length) return cachedState;
  const firstSeenAt = { ...cachedState.firstSeenAt, ...fresh };
  cachedState = pruneByAgeAndSize({ ...cachedState, firstSeenAt }, now, new Set(presentKeys));
  Object.assign(pendingStamps, fresh);
  if (stampTimer === null) stampTimer = setTimeout(flushStamps, STAMP_FLUSH_MS);
  emit();
  return cachedState;
}

/** Mark every one of `keys` seen — called on panel close with every key that
 *  was present at any point during that open (R4). A message key goes into
 *  the in-memory set; everything else is merged into `seen` and posted at
 *  once, along with any stamps still waiting on the coalesced flush. Pruning
 *  by age/size happens only in `stampFirstSeen` (the regular per-render write
 *  path) — not here, since this is the one call site that does not carry the
 *  caller's own notion of "now", and reusing `Date.now()` here would prune
 *  entries stamped under a test's (or a future caller's) synthetic clock.
 *
 *  `useSeenSnapshot` is a `useSyncExternalStore` read, which skips the
 *  re-render it would otherwise trigger whenever two consecutive
 *  `getSnapshot()` calls are `Object.is`-equal. A message-only change here
 *  used to leave `cachedState` (and so `snapshot`) pointing at the exact
 *  same object — `messageSeenSet` had changed, but nothing a subscriber's
 *  `getSnapshot()` returns had, so a chip relying only on this store's own
 *  notification never refreshed its unseen count. `added` tracks the
 *  server-bound keys (worth a POST), separately from `anyChanged` (worth a
 *  fresh snapshot object and an `emit`, messages included) — so a
 *  message-only call still gets a new reference without a POST, and a call
 *  that changes nothing at all still returns the identical reference
 *  `getSnapshot` must keep stable. */
export function markSeen(keys: readonly string[]): void {
  let anyChanged = false;
  const seenSet = new Set(cachedState.seen);
  const added: string[] = [];
  for (const key of keys) {
    if (isMessageKey(key)) {
      if (!messageSeenSet.has(key)) {
        messageSeenSet.add(key);
        anyChanged = true;
      }
    } else if (!seenSet.has(key)) {
      seenSet.add(key);
      added.push(key);
      anyChanged = true;
    }
  }
  if (!anyChanged) return;
  cachedState = added.length ? { ...cachedState, seen: [...seenSet] } : { ...cachedState };
  emit();
  if (added.length) {
    const firstSeenAt = pendingStamps;
    pendingStamps = {};
    if (stampTimer !== null) clearTimeout(stampTimer);
    stampTimer = null;
    post(Object.keys(firstSeenAt).length ? { seen: added, firstSeenAt } : { seen: added });
  }
}

/** Dismiss one repo row at its current `repoDismissSignature`. Keyed on the
 *  signature, never on `checked_at` (D584 finding 3): a re-check that changes
 *  nothing must not resurrect a dismissed row. */
export function dismissRepo(root: string, signature: string): void {
  dismissRepoMany({ [root]: signature });
}

/** Dismiss several repo rows in one write — the "dismiss all" button. */
export function dismissRepoMany(entries: Record<string, string>): void {
  const changed = Object.entries(entries).filter(
    ([root, sig]) => cachedState.repoDismissed[root] !== sig,
  );
  if (!changed.length) return;
  const patch = Object.fromEntries(changed);
  cachedState = { ...cachedState, repoDismissed: { ...cachedState.repoDismissed, ...patch } };
  emit();
  post({ repoDismissed: patch });
}

/** Dismiss one waiting-task row at its current `attentionDismissSignature`. */
export function dismissAttention(key: string, signature: string): void {
  if (cachedState.attentionDismissed[key] === signature) return;
  cachedState = {
    ...cachedState,
    attentionDismissed: { ...cachedState.attentionDismissed, [key]: signature },
  };
  emit();
  post({ attentionDismissed: { [key]: signature } });
}

// ---- test hooks ----------------------------------------------------------

/** Test-only reset so suites don't leak state into one another. Seeds the
 *  cache with `initial` (a hydrated server record, say) or empties it; clears
 *  the in-memory message set, any pending coalesced stamps, and the hydration
 *  memo (an in-flight hydration from an earlier test is fenced off by
 *  `epoch`). Not used by any non-test caller.
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
export function _resetSeenStoreForTest(initial?: Partial<NotificationsState>): void {
  epoch++;
  hydration = null;
  messageSeenSet.clear();
  if (stampTimer !== null) clearTimeout(stampTimer);
  stampTimer = null;
  pendingStamps = {};
  cachedState = initial ? { ...EMPTY, ...normalize({ ...EMPTY, ...initial }) } : EMPTY;
  listeners.clear();
  emit();
}

/** Test-only: the exact object `useSeenSnapshot`'s `useSyncExternalStore`
 *  would read right now. Lets a test assert on snapshot IDENTITY directly —
 *  `useSyncExternalStore` bails out of re-rendering a subscriber whenever two
 *  consecutive `getSnapshot()` calls are `Object.is`-equal, so a `markSeen`
 *  that changes seen state but returns the SAME reference silently drops
 *  that update on the floor for any subscriber not re-rendering for some
 *  other reason. Not used by any non-test caller. */
export function _currentSnapshotForTest(): NotificationsState {
  return snapshot;
}
