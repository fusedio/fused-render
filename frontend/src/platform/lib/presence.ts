// THE PRESENCE REGISTRY (SPEC-quiet-notifications.md §1) — "is the thing this
// notification is about already on screen somewhere?" Nothing in this
// codebase tracked that before this file: every notification is fire-and-
// forget regardless of whether the user is already looking at the result.
//
// SHAPE borrowed from `apps/claude/ui/useAwayRecap.ts` (visibilitychange +
// blur/focus, an injectable `now`/env for tests) — not imported, since that
// file is under `apps/` and `platform/` may not import `apps/`
// (`frontend/scripts/check-boundaries.mjs`).
//
// CROSS-WINDOW TRANSPORT is `localStorage` + the native `storage` event —
// the established idiom (`platform/lib/jobs.ts`'s `JOB_PING_KEY`, read in
// `platform/ui/DownloadManager.tsx`), not `BroadcastChannel` and not
// `postMessage` (this repo deliberately avoids the latter — see
// `notifications.ts`'s own header comment on why it uses a same-origin
// `window.top` global instead for the parent/child case).
import { NAV_EVENT, currentUrl, fsPathFromLocation, IS_EMBED } from "@platform/lib/router";

export interface PresenceEntry {
  page: string;
  focused: boolean;
  ts: number;
  // Not in the spec's own minimal shape, but needed to answer "narrator
  // election" (§1's last paragraph) honestly: election is over TOP-LEVEL
  // windows only, and a pane's own entry (every pane registers too) must not
  // be eligible to narrate. Recorded per-entry rather than inferred from
  // `page` because a pane and its top-level tab can show the same page.
  topLevel: boolean;
}

const STORAGE_KEY = "fused-render:presence";

/** How often a live document re-stamps its own entry. */
export const PRESENCE_REFRESH_MS = 5_000;

/** An entry older than this is ignored on read and dropped on write — a
 *  window closed without running its `pagehide` cleanup (a crash, a killed
 *  process, a test that never tears down) must not suppress notifications
 *  forever. 3x the refresh interval, per the spec's own "~3x" guidance:
 *  wide enough that one missed heartbeat under load isn't mistaken for a
 *  closed window, narrow enough that a real close is forgotten in seconds,
 *  not minutes. */
export const PRESENCE_STALE_MS = PRESENCE_REFRESH_MS * 3;

/** The seam every read/write goes through — real `localStorage`/`Date.now`
 *  by default, overridable so tests can exercise staleness and the
 *  throws-on-read/write case without touching a real store. */
export interface PresenceEnv {
  storage?: Pick<Storage, "getItem" | "setItem" | "removeItem"> | null;
  now?: () => number;
}

function realStorage(): Pick<Storage, "getItem" | "setItem" | "removeItem"> | null {
  try {
    return typeof localStorage === "undefined" ? null : localStorage;
  } catch {
    // Thrown in a private window or with site data blocked — see this
    // module's header: degrade to "nothing on record" (which reads as
    // "notify"), never crash the caller mid-`notify()`/mid-`jobRows()`.
    return null;
  }
}

function storageOf(env: PresenceEnv): Pick<Storage, "getItem" | "setItem" | "removeItem"> | null {
  return env.storage !== undefined ? env.storage : realStorage();
}

function nowOf(env: PresenceEnv): number {
  return (env.now ?? Date.now)();
}

function readAll(env: PresenceEnv): Record<string, PresenceEntry> {
  const storage = storageOf(env);
  if (!storage) return {};
  try {
    const raw = storage.getItem(STORAGE_KEY);
    if (!raw) return {};
    const parsed: unknown = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object") return {};
    return parsed as Record<string, PresenceEntry>;
  } catch {
    // A throwing read (blocked storage) or a corrupt value either one reads
    // as "nobody has anything open" — never as "everybody does".
    return {};
  }
}

function writeAll(map: Record<string, PresenceEntry>, env: PresenceEnv): void {
  const storage = storageOf(env);
  if (!storage) return;
  try {
    storage.setItem(STORAGE_KEY, JSON.stringify(map));
  } catch {
    // Best-effort. A write that fails leaves other windows reading a stale
    // (or absent) entry for this one, which — same direction as every other
    // failure here — degrades to "notify", not to a crash.
  }
}

function isStale(entry: PresenceEntry, now: number): boolean {
  return typeof entry.ts !== "number" || now - entry.ts > PRESENCE_STALE_MS;
}

function pruneStale(map: Record<string, PresenceEntry>, now: number): Record<string, PresenceEntry> {
  const next: Record<string, PresenceEntry> = {};
  for (const [id, entry] of Object.entries(map)) {
    if (entry && !isStale(entry, now)) next[id] = entry;
  }
  return next;
}

// ---- source matching --------------------------------------------------

/** Is `page` (what a window is currently showing) the same "place" as
 *  `source` (what a job or message names as its origin)? Two shapes, each
 *  needing a different rule:
 *
 *  - A query-bearing shell route (`/preferences?tab=lan`) must match ONLY
 *    the exact same route+query — `/preferences?tab=lan` is not "open" just
 *    because `/preferences?tab=indexing` is. Neither side is ever read as a
 *    prefix of the other once either one carries a `?`.
 *  - Everything else (a bare shell route, or an fs path) matches by prefix
 *    on a path boundary: an app folder counts as open when a window is
 *    showing anything nested under it (`/a/project` vs. a window on
 *    `/a/project/sub/file.py`), and the reverse also counts — a window
 *    sitting exactly on a sub-path still has the parent folder "open" for
 *    the purpose of a notification raised at that parent. The `+ "/"` guard
 *    is what keeps `/a/project` from matching `/a/project-2`. */
export function matchesSource(page: string, source: string): boolean {
  if (!page || !source) return false;
  if (page === source) return true;
  if (page.includes("?") || source.includes("?")) return false;
  return page.startsWith(source + "/") || source.startsWith(page + "/");
}

// ---- current document's own "page" -------------------------------------

/** What THIS document would write as its own `page` — an fs path when it's
 *  showing one (`/explorer/view/...`, `/explorer/embed/...`), else the shell
 *  route + query as-is (so `/preferences?tab=lan` round-trips exactly). */
export function currentPresencePage(): string {
  try {
    return fsPathFromLocation() ?? currentUrl();
  } catch {
    return "";
  }
}

function isFocusedAndVisible(): boolean {
  try {
    if (typeof document === "undefined") return false;
    if (typeof document.hasFocus === "function" && !document.hasFocus()) return false;
    if (document.visibilityState !== undefined && document.visibilityState !== "visible") return false;
    return true;
  } catch {
    return false;
  }
}

// ---- exported predicates ------------------------------------------------

/** Any non-stale window or pane — anywhere — currently showing `source`. */
export function isOpenAnywhere(source: string, env: PresenceEnv = {}): boolean {
  const now = nowOf(env);
  const map = pruneStale(readAll(env), now);
  return Object.values(map).some((e) => matchesSource(e.page, source));
}

/**
 * Finding 6: `isOpenAnywhere` does a synchronous `localStorage.getItem` +
 * `JSON.parse` on every single call. A poll tick in ActivityDock calls it
 * once per terminal job, once per recent job, once per popup candidate, and
 * (via the grouped variants in jobs.ts) once per member of every
 * multi-member group — all within the same tick, all against the exact same
 * underlying registry snapshot. That's an O(N)-ish pile of synchronous
 * main-thread storage reads/parses per tick for data that hasn't changed
 * since the top of the tick.
 *
 * Call this ONCE per tick and pass the returned predicate anywhere an
 * `isOpenAnywhere`-shaped function is expected (it has the same
 * `(source: string) => boolean` signature) — every caller downstream
 * (terminalNotifications, recentNotifications, popupTick, groupPopupTick,
 * and their internal per-member checks) then reuses the one read.
 */
export function snapshotIsOpenAnywhere(env: PresenceEnv = {}): (source: string) => boolean {
  const now = nowOf(env);
  const pages = Object.values(pruneStale(readAll(env), now)).map((e) => e.page);
  return (source: string) => pages.some((page) => matchesSource(page, source));
}

/** Only THIS document: is it showing `source`, focused, and visible right
 *  now? Deliberately does not consult the registry at all — a document
 *  always knows its own state precisely, and going through localStorage
 *  (round-tripped through JSON, on a refresh cadence) would make this
 *  document's own answer stale by up to `PRESENCE_REFRESH_MS`. */
export function isFocusedHere(source: string, env: Pick<PresenceEnv, "now"> = {}): boolean {
  void env;
  if (!isFocusedAndVisible()) return false;
  return matchesSource(currentPresencePage(), source);
}

// ---- narrator election ----------------------------------------------------

/** Lowest non-stale `windowId` among TOP-LEVEL entries narrates schedule and
 *  task events (§1's own reasoning: two top-level tabs each polling
 *  independently each popped their own card — this is how exactly one of
 *  them gets to). Panes are never eligible, even though every pane
 *  registers (a pane is a place the user can be looking, but it is not a
 *  window of its own to narrate from). */
export function isNarrator(env: PresenceEnv = {}): boolean {
  const now = nowOf(env);
  const map = pruneStale(readAll(env), now);
  const topLevelIds = Object.keys(map)
    .filter((id) => map[id].topLevel)
    .sort();
  if (topLevelIds.length === 0) return true; // nobody on record — degrade to "yes", never to silence
  return topLevelIds[0] === windowId;
}

// ---- this document's own heartbeat ---------------------------------------

// Minted once per document (module-scope singleton, exactly like every
// other per-document id this codebase mints — e.g. `notifications.ts`'s own
// `nextId` sequence, though that one restarts per document on purpose; this
// one must NOT collide with another document's, hence the random suffix).
const windowId = `w${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`;

// Finding 1 (code review 2026-09-16): this used to be
// `IS_TOP_EMBED || window === window.top` — but `IS_TOP_EMBED` (an EMBED
// document that also happens to be top-level) already IMPLIES
// `window === window.top` by its own definition, so that `||` was a no-op
// that changed nothing: any top-level window, embed or not, registered as
// `topLevel: true`. An embed/preview document opened standalone in its own
// tab is exactly that — top-level, and eligible to WIN the narrator
// election — but nothing ever narrates from an embed (`useScheduleEvents`
// bails out on `IS_EMBED` before it ever calls `isNarrator()`, and
// `useTaskStatusNotify` only mounts inside the shell's `App`). If that
// embed's `windowId` sorts first, the real shell tab loses the election to
// a document that will never narrate, silencing every schedule/task
// notification for as long as the embed tab stays open. Narrator
// eligibility must mean "a document that could actually narrate" —
// `isEmbed` is excluded outright, not folded into an `||` that never
// mattered.
//
// Pulled out as a pure function (rather than inlined in `writeSelf`) so a
// test can pin the exclusion directly against both `isEmbed` values without
// having to vary `IS_EMBED`, which `router.ts` only ever computes once, at
// module load, from `location.pathname`.
export function computeTopLevel(isEmbed: boolean, win: Window | undefined): boolean {
  return !isEmbed && (typeof win === "undefined" || win === win.top);
}

function writeSelf(env: PresenceEnv = {}): void {
  const now = nowOf(env);
  const map = pruneStale(readAll(env), now);
  map[windowId] = {
    page: currentPresencePage(),
    focused: isFocusedAndVisible(),
    ts: now,
    topLevel: computeTopLevel(IS_EMBED, typeof window === "undefined" ? undefined : window),
  };
  writeAll(map, env);
}

function removeSelf(env: PresenceEnv = {}): void {
  const map = readAll(env);
  if (!(windowId in map)) return;
  delete map[windowId];
  writeAll(map, env);
}

/** Test-only — lets a suite drive the heartbeat without waiting on real
 *  timers/events. Not used by any non-test caller. */
export function _writePresenceForTest(env: PresenceEnv = {}): void {
  writeSelf(env);
}
/** Test-only — see `_writePresenceForTest`. */
export function _removePresenceForTest(env: PresenceEnv = {}): void {
  removeSelf(env);
}
/** Test-only — this document's own minted id, so a test can plant a sibling
 *  entry under a *different* key and assert against a known "self" id. */
export function _presenceWindowIdForTest(): string {
  return windowId;
}

// Wired at module load, the same self-installing pattern
// `notifications.ts`'s `installIngest()` uses — there is no other call site
// that would reliably run once per document, and every document that can
// `import` this module is a document worth registering.
//
// TIMER THROUGH `globalThis`, not `window` — `notifications.ts`'s own rule
// (see its header comment), for the same reason: this module is imported
// transitively by plenty of test files that never test presence itself and
// stub only a minimal `window` (no `setInterval`, sometimes no
// `addEventListener` at all — `RepoUpdatesDock.test.tsx` is exactly this
// case). Every listener registration below is individually guarded rather
// than gated on one up-front feature check, so a shim missing ONE member
// (say, `addEventListener` but not `dispatchEvent`) still gets every other
// listener it can support.
function safeListen(
  target: { addEventListener?: (type: string, fn: () => void) => void } | undefined | null,
  type: string,
  fn: () => void,
): void {
  try {
    target?.addEventListener?.(type, fn);
  } catch {
    // A shim whose addEventListener itself throws (none currently do, but
    // nothing here may assume otherwise) degrades to "this document just
    // never updates that entry" — never to a crash at import time, which
    // would take down every OTHER file in the same `bun test` process.
  }
}

function installHeartbeat(): void {
  if (typeof window === "undefined") return;
  try {
    writeSelf();
  } catch {
    // Same degrade-don't-crash rule as every read/write above.
  }
  globalThis.setInterval(() => writeSelf(), PRESENCE_REFRESH_MS);
  safeListen(window, "focus", () => writeSelf());
  safeListen(window, "blur", () => writeSelf());
  safeListen(window, NAV_EVENT, () => writeSelf());
  safeListen(window, "pagehide", () => removeSelf());
  safeListen(window, "beforeunload", () => removeSelf());
  safeListen(typeof document === "undefined" ? null : document, "visibilitychange", () => writeSelf());
}
installHeartbeat();
