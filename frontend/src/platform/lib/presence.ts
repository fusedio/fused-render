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
import { ORIGIN_BY_ROUTE } from "@platform/lib/originRoutes";

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
  // Added for F9 (code review of F8's "already open" gate): a hover-preview
  // (`BookmarkCards.tsx`'s `LivePreview`, `AppPreviewCard.tsx`) loads the
  // shell at `/explorer/embed/<path>` in an iframe purely to render a
  // thumbnail, and that document runs this same heartbeat (`installHeartbeat`
  // below is module-load, unconditional) — so a hovered card publishes
  // presence for the path it previews, indistinguishable from a real open
  // window until this field existed. `IS_EMBED` was already computed at
  // write time (for `topLevel`, above) but never stored on the entry itself,
  // which is what made this gap possible: nothing downstream could tell a
  // genuine standalone embed tab OR a transient preview iframe apart from an
  // ordinary shell window. This is the smallest addition that fixes it —
  // one boolean, set once at `writeSelf` — rather than inventing a second,
  // narrower "is this truly just a thumbnail" signal that would need a
  // change at every embed call site to plumb through.
  embed: boolean;
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

function parseRegistry(raw: string | null): Record<string, PresenceEntry> {
  if (!raw) return {};
  try {
    const parsed: unknown = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object") return {};
    return parsed as Record<string, PresenceEntry>;
  } catch {
    // A corrupt value reads as "nobody has anything open" — never as
    // "everybody does".
    return {};
  }
}

function safeGetItem(storage: Pick<Storage, "getItem" | "setItem" | "removeItem">): string | null {
  try {
    return storage.getItem(STORAGE_KEY);
  } catch {
    // A throwing read (blocked storage) degrades the same way a corrupt
    // value does — see `parseRegistry`.
    return null;
  }
}

function readAll(env: PresenceEnv): Record<string, PresenceEntry> {
  const storage = storageOf(env);
  if (!storage) return {};
  return parseRegistry(safeGetItem(storage));
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

/** How many times `mutateRegistry` retries a mutation whose commit lost the
 *  race to a concurrent write from another document — see that function's
 *  own doc for what "the race" means. A handful of attempts is plenty:
 *  contention this tight, repeated this many times in a row, is vanishingly
 *  unlikely outside of a test deliberately engineering it. */
const MAX_MUTATE_ATTEMPTS = 5;

/** Finding 10 (code review 2026-09-16): `writeSelf`/`removeSelf` used to do a
 *  plain read-modify-write against the WHOLE registry — read the map, change
 *  only this document's own entry, write the whole map back — with nothing
 *  guarding against another document doing the exact same thing to a
 *  DIFFERENT entry in between. Concretely: window A's `pagehide` reads
 *  `{A, B}`, deletes its own entry, and is about to write `{B}` back.
 *  Meanwhile window B's periodic heartbeat reads `{A, B}` — still, if this
 *  happens before A's write actually lands — updates its own entry's
 *  timestamp, and writes `{A(stale), B(refreshed)}` back. If B's write lands
 *  after A's, A's already-closed entry is resurrected in the registry, and
 *  stays there — wrongly counted as "open" — until it eventually ages out
 *  via `PRESENCE_STALE_MS`.
 *
 *  Plain `localStorage` has no compare-and-swap, so this can only be
 *  narrowed, not eliminated outright: read the raw string once, compute the
 *  mutated map from it, then re-read the raw string immediately before
 *  writing. If it still matches what the mutation started from, nothing else
 *  touched the registry in between and it is safe to commit. If it doesn't,
 *  someone else's write landed in that window — retry the whole mutation
 *  against THAT fresher snapshot instead of blindly overwriting it. This
 *  shrinks the vulnerable window from "the entire read-then-write" down to
 *  "the single instant between the verification read and the `setItem`
 *  call" — the same order-of-magnitude reduction a plain optimistic-lock
 *  retry buys anywhere else two writers share one resource with no native
 *  locking primitive.
 *
 *  `transform` receives the freshest known map and returns the map to
 *  commit — `writeSelf` prunes and upserts its own entry; `removeSelf`
 *  deletes its own entry (or returns the map unchanged if it was never
 *  there, so a no-op removal never even attempts a write). */
function mutateRegistry(
  transform: (current: Record<string, PresenceEntry>) => Record<string, PresenceEntry>,
  env: PresenceEnv,
): void {
  const storage = storageOf(env);
  if (!storage) return;
  let before = safeGetItem(storage);
  let current = parseRegistry(before);
  let next = transform(current);
  // `transform` returning the SAME reference it was handed (see
  // `removeSelf`'s own no-op path) means there is nothing to commit at all —
  // never even attempt a write, exactly like the pre-fix code's early
  // `return` for "I was never in the map".
  if (next === current) return;
  for (let attempt = 0; attempt < MAX_MUTATE_ATTEMPTS; attempt++) {
    const check = safeGetItem(storage);
    if (check === before) {
      writeAll(next, env);
      return;
    }
    // Someone else's write landed between our read and our verification —
    // retry the mutation against the fresher snapshot rather than
    // clobbering it.
    before = check;
    current = parseRegistry(before);
    next = transform(current);
    if (next === current) return;
  }
  // Contention this persistent (every single attempt raced) is not worth
  // spinning on forever: commit against the last snapshot seen rather than
  // silently dropping this document's own update.
  writeAll(next, env);
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

// ---- canonical identity --------------------------------------------------

/** Defect 1 (live testing, 2026-09-17): `currentPresencePage()` used to
 *  return `currentUrl()` verbatim — pathname + query, exactly as the address
 *  bar would show it. That is fine for a route whose query is genuinely part
 *  of "which page is this" (a Preferences tab), but most query-bearing shell
 *  routes carry APP STATE, not identity: the Playground syncs its prompt and
 *  model into the query string on every keystroke-ish change, so two
 *  requests seconds apart from the SAME open tab produced two different
 *  `page`/`source` values. Two consequences, both observed live:
 *   - `familyKey` (`jobs.ts`) is `job.source || job.page`, so the two
 *     text-gen rows never shared a key and could never group.
 *   - `matchesSource` requires an exact match once either side carries a
 *     `?`, so the row's own `source` (captured when the request was made)
 *     almost never exact-matched the CURRENT presence page by the time the
 *     job finished — suppression silently never fired, which is very likely
 *     why popups kept appearing for a page the user never left.
 *
 *  THE RULE: a query-bearing string's identity is the string itself ONLY
 *  when it is a registered key in `ORIGIN_BY_ROUTE` — that table is already
 *  the closed set of "this exact route+query is its own distinct surface"
 *  (`/preferences?tab=indexing` beside bare `/preferences`, Fix 19).
 *  Everything else canonicalizes down to the bare path: a Playground prompt,
 *  a model id, `_side=claude`, a `session_id` are shell/app state, never
 *  identity, and dropping the query (and any hash) is what makes two
 *  requests from the same open tab collapse to the same value.
 *
 *  APPLIED IN ONE PLACE per consumer, not three: `currentPresencePage()`
 *  (below) canonicalizes what THIS document stamps into its own presence
 *  entry and what `api.ts`'s `ambientSourceHeaders()` sends as
 *  `X-Fused-Source` — the same value `familyKey` groups by once the server
 *  echoes it back as `Job.source`. `matchesSource` ALSO canonicalizes both
 *  of its arguments (not just trusts its callers to have already done so),
 *  because a `Job.source` already sitting in the store from before this fix
 *  shipped is exactly the "dirty" value this bug produced — canonicalizing
 *  at compare-time lets an old dirty row still match a freshly-canonical
 *  live presence page instead of being stuck matching a URL nobody will
 *  ever show again. */
function canonicalPresenceIdentity(value: string): string {
  if (Object.prototype.hasOwnProperty.call(ORIGIN_BY_ROUTE, value)) return value;
  const qIdx = value.indexOf("?");
  const hIdx = value.indexOf("#");
  const cut = Math.min(qIdx === -1 ? value.length : qIdx, hIdx === -1 ? value.length : hIdx);
  return value.slice(0, cut);
}

// ---- source matching --------------------------------------------------

/** Is `page` (what a window is currently showing) the same "place" as
 *  `source` (what a job or message names as its origin)? Two shapes, each
 *  needing a different rule:
 *
 *  - A query-bearing shell route that is a registered `ORIGIN_BY_ROUTE` key
 *    (`/preferences?tab=indexing`) must match ONLY the exact same
 *    route+query — it is not "open" just because bare `/preferences` is.
 *    Once canonicalization (above) has reduced every OTHER query-bearing
 *    string down to its bare path, this is the only shape that can still
 *    reach here carrying a `?`, so the rule below still earns its keep: it
 *    stops that registered surface from cross-matching, or being
 *    prefix-matched against, the bare route it sits beside.
 *  - Everything else (a bare shell route, or an fs path) matches by prefix
 *    on a path boundary: an app folder counts as open when a window is
 *    showing anything nested under it (`/a/project` vs. a window on
 *    `/a/project/sub/file.py`), and the reverse also counts — a window
 *    sitting exactly on a sub-path still has the parent folder "open" for
 *    the purpose of a notification raised at that parent. The `+ "/"` guard
 *    is what keeps `/a/project` from matching `/a/project-2`. */
export function matchesSource(page: string, source: string): boolean {
  if (!page || !source) return false;
  const p = canonicalPresenceIdentity(page);
  const s = canonicalPresenceIdentity(source);
  if (p === s) return true;
  if (p.includes("?") || s.includes("?")) return false;
  return p.startsWith(s + "/") || s.startsWith(p + "/");
}

// ---- current document's own "page" -------------------------------------

/** What THIS document would write as its own `page` — an fs path when it's
 *  showing one (`/explorer/view/...`, `/explorer/embed/...`; an fs path
 *  never carries a query, so it needs no canonicalization), else the shell
 *  route canonicalized per `canonicalPresenceIdentity` above (so
 *  `/preferences?tab=indexing` round-trips exactly, while
 *  `/ai-models/playground?prompt=...&model=...` round-trips as bare
 *  `/ai-models/playground`). */
export function currentPresencePage(): string {
  try {
    return fsPathFromLocation() ?? canonicalPresenceIdentity(currentUrl());
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

/**
 * F9 (code review of F8's "already open" popup gate, `task-status-notify.ts`).
 * `matchesSource`'s bidirectional prefix rule ("an ancestor folder counts as
 * open, and so does a descendant") is right for `isOpenAnywhere`'s existing
 * callers (`jobs.ts`'s `isPopupSuppressed`: a job running somewhere under an
 * open folder tab IS "already being watched"), but far too wide for "is this
 * task's own destination open": a browser tab merely sitting on an ANCESTOR
 * of a task's folder (e.g. `/Fused/sandbox`) would suppress the popup for
 * EVERY task nested anywhere beneath it, which has nothing to do with that
 * specific task's own app/chat being on screen. `matchesSource` itself is
 * left untouched — other callers depend on the wider rule — so this is a
 * SEPARATE snapshot function with its own, stricter comparison (exact
 * canonical-string match only) rather than a flag threaded through the
 * shared one.
 *
 * Also excludes any entry with `embed: true` — a hover-preview/peek iframe
 * (`BookmarkCards.tsx`, `AppPreviewCard.tsx`) runs the same heartbeat purely
 * to render a thumbnail, and a thumbnail must never count as "the app is
 * open" (see `PresenceEntry.embed`'s own doc comment). This is the ONE
 * caller that needs that exclusion today — `isOpenAnywhere`/
 * `snapshotIsOpenAnywhere` keep counting embed entries, unchanged, for every
 * other consumer.
 */
export function snapshotIsOpenExact(env: PresenceEnv = {}): (page: string) => boolean {
  const now = nowOf(env);
  const openPages = new Set(
    Object.values(pruneStale(readAll(env), now))
      .filter((e) => !e.embed)
      .map((e) => e.page),
  );
  return (page: string) => openPages.has(page);
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
  mutateRegistry((current) => {
    const map = pruneStale(current, now);
    map[windowId] = {
      page: currentPresencePage(),
      focused: isFocusedAndVisible(),
      ts: now,
      topLevel: computeTopLevel(IS_EMBED, typeof window === "undefined" ? undefined : window),
      embed: IS_EMBED,
    };
    return map;
  }, env);
}

function removeSelf(env: PresenceEnv = {}): void {
  mutateRegistry((current) => {
    if (!(windowId in current)) return current;
    const map = { ...current };
    delete map[windowId];
    return map;
  }, env);
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
