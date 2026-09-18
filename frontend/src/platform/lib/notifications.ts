// Global notification store — replaces `lib/toast`. A client-raised message
// (a failure, a completed op) now shares the exact pop-then-retain lifetime
// the job system already has (`JobTier`, this module's own import): it pops a
// card in `.notif-host` for `JOB_POPUP_VISIBLE_MS`, then either leaves for
// good, or is retained in the Notifications panel until the user dismisses
// it — retained only if it is an error, or carries something to act on
// (`isRetained` below) — see SPEC-toasts-become-notifications.md for the
// full reasoning and DECISIONS-toasts-become-notifications.md for the
// call-by-call tier table and the later retention-narrowing reversal.
//
// SAME module-store + useSyncExternalStore SHAPE `toast.ts` used — that part
// was never the problem, so it is kept verbatim: mutations update a module
// array/value and notify subscribers, NotificationHost/RepoUpdatesDock re-read
// on every change.
//
// ONE POPUP AT A TIME, latest wins — mirrors `jobs.ts`'s own `popupTick`
// ("the latest notification always pops up", not a queue of them), NOT
// `toast.ts`'s old MAX_TOASTS=5 simultaneous stack. That old stack existed
// only because nothing ever auto-dismissed; now that every popup clears
// itself in ~2.65s, stacking is no longer what multiple arrivals need — a
// second `notify()` simply replaces whatever is still popping, exactly like a
// second terminal job replaces the first job's own popup.
//
// THE RETAINED LIST is the part with no toast.ts precedent: a message stays
// here — capped, like the old stack, at MAX_RETAINED — until
// `dismissNotification` (or the panel's "Clear all") removes it. Retention
// narrowed from "attention or trail" to "attention, or carries an action/page"
// (see `isRetained` below and DECISIONS-toasts-become-notifications.md) —
// everything else is never added to it; its popup is its only trace.
import { useSyncExternalStore } from "react";
import { JOB_POPUP_VISIBLE_MS } from "@platform/lib/jobs";
import type { JobTier } from "@platform/lib/jobs";
import { IS_EMBED, IS_TOP_EMBED } from "@platform/lib/router";
import { isFocusedHere } from "@platform/lib/presence";
import { labelForSource } from "@platform/lib/format";
import type { NotificationCardAction } from "@platform/ui/NotificationCard";

// `labelForSource` LIVES IN format.ts, not here — see that file's own header
// comment for why (this module imports router.ts, which reads `location` at
// module scope; format.ts has zero imports/side effects so `tasks-lib.ts`
// can reach it without dragging that chain in). Re-exported so existing
// `notify()` callers/tests that reach it via this module keep working.
export { labelForSource } from "@platform/lib/format";

// "trail" is deliberately UNREPRESENTABLE on client input — see
// DECISIONS-toasts-become-notifications.md's "Retention narrows to error-or-
// actionable" entry): a bare "kept in the panel" tier meant something for a
// client-raised message when `trail` alone was enough to retain it, but that
// reading no longer exists — retention is now `resolveTier(...) ===
// "attention"` OR the message carries a destination (`action`/`page`). A
// caller that types `tier: "trail"` today is trying to say "keep this
// around" the OLD way; excluding it from the type turns that mistake into a
// compile error instead of a silently-wrong runtime no-op. `jobs.ts`'s own
// `JobTier` (the server/job vocabulary `effectiveTier` reads) is untouched —
// a server-side job row still uses `trail` exactly as before; only the
// client `notify()` input narrows.
export type ClientNotificationTier = Exclude<JobTier, "trail">;

export interface NotificationInput {
  title: string;
  detail?: string;
  /** An explicit tier wins over the tone-derived default — UNLESS `tone` is
   *  "error", which always promotes to "attention" regardless (see
   *  `resolveTier` below; mirrors `jobs.ts`'s `effectiveTier` promoting any
   *  error/cancelled JOB to "attention" no matter what its producer
   *  declared). Does NOT accept "trail" — see `ClientNotificationTier`. */
  tier?: ClientNotificationTier;
  /** Retained as the ergonomic shorthand every call site already used. */
  tone?: "error" | "info";
  action?: NotificationCardAction;
  /** A second, independent destination — `NotificationCard`'s own
   *  `extraAction` slot (the "Fix with Claude" style quiet `.q-all` button
   *  below the status line). A caller with only one thing to offer uses
   *  `action` alone; this is for the rare row with two (a saved export's
   *  "Open file" alongside `action`'s "Reveal folder"). */
  extraAction?: NotificationCardAction;
  /** Click destination for the retained panel row (SPEC
   *  actionable-notifications' "every row goes somewhere"). Unused by the
   *  popup card. */
  page?: string;
  /** OPT-IN ONLY (SPEC-quiet-notifications.md §2a) — the page/route this
   *  message is ABOUT, for the "you're already looking at this" suppression
   *  check ONLY (`isSuppressed` below). Deliberately never defaulted to the
   *  raising document's own current page: that would suppress "Path copied"
   *  and every other gesture confirmation whose only feedback IS the card.
   *  Set this only where the page already shows the same result on screen
   *  (the app install/run lifecycle messages this branch wires it for) —
   *  and where a caller genuinely wants a finished/still-visible source to
   *  go quiet. A caller that wants the "who raised this" caption WITHOUT
   *  that suppression meaning (a task that already ended, so "the chat is
   *  open" no longer means "already knows") sets `origin` instead — see its
   *  own comment for why the two are not the same field. `toStored` still
   *  falls back to `labelForSource(source)` when no explicit `origin` is
   *  given, so every existing `source`-only caller keeps its caption exactly
   *  as before. */
  source?: string;
  /** THE CAPTION, INDEPENDENT OF SUPPRESSION (2026-09-17 fix). Pre-computed
   *  by the caller (typically `labelForSource(...)` — see `format.ts`) and
   *  drawn verbatim as `.dl-origin`'s eyebrow text via `toStored`. This
   *  field exists because `source` used to do BOTH jobs at once — "caption
   *  this row" AND "suppress it when its page is open" — and the two are
   *  not the same question. Removing `source` from a call site (as
   *  `task-status-notify.ts`'s `in_progress -> done` branch did, correctly,
   *  to stop presence-suppressing a FINISHED task) silently deleted the
   *  caption too, because nothing else fed `toStored`'s caption computation.
   *  A caller sets `origin` whenever it wants the "who made this" label
   *  without opting into suppression; it sets `source` (with no `origin`)
   *  when the old "caption AND suppress" pairing is what it actually wants;
   *  it can set both if a future case genuinely needs a caption computed
   *  differently from the suppression key. Never string-empty on purpose —
   *  pass `undefined`, not `""`, when there is nothing to show (`toStored`
   *  treats `""` the same as absent either way, but an explicit `undefined`
   *  reads honestly at the call site). */
  origin?: string;
  /** OPT-IN family override (2026-09-18 fix, "these 2 fused-render
   *  notifications should have been grouped together as count"). Default
   *  collapse identity is caption+TITLE (`messageFamily` below) — correct
   *  for the general case (an unrelated error and an unrelated info message
   *  in the same folder must stay two rows), but wrong for a finished-task
   *  notice: two finished runs in the same folder ("hi", "New session") share
   *  a caption but never share a title, so they never collapsed, which is
   *  the exact bug this field exists to fix. Rather than loosen the default
   *  key for every caller (an unrelated error/info pair sharing a folder
   *  would then wrongly merge too), a caller that wants collapse coarser
   *  than "caption+title" sets this explicitly; `messageFamily` prefers it
   *  outright over the caption/page/title chain when present. Only
   *  `task-status-notify.ts`'s `in_progress -> done` branch sets it today —
   *  see its own comment. TRADE-OFF: because the collapsed row is always
   *  rebuilt from the LATEST input (same as the existing per-run-page
   *  collapse above), a folder's newest finished task overwrites the title
   *  of whatever finished task was shown before it — "hi" then "New session"
   *  finishing in the same folder shows "New session" with `count: 2`, not
   *  both titles. Accepted for the same reason the per-run-page collapse
   *  already accepts it: `count` carries "how many", the row's job is to
   *  point at what's most likely to matter now (the newest one). */
  familyKey?: string;
}

export interface StoredNotification {
  id: number;
  title: string;
  detail?: string;
  tier: JobTier;
  tone?: "error" | "info";
  action?: NotificationCardAction;
  extraAction?: NotificationCardAction;
  page?: string;
  /** GROUPING/UPDATION (user: "better notification grouping/updation for
   *  same source") — the family this row belongs to: `page:<page>::<title>`
   *  when it has a page, else `title:<title>`. Title is part of the key even
   *  on the page branch (2026-09-17 fix) — `page` alone collided whenever two
   *  DIFFERENT tasks fell back to the same per-folder/global destination
   *  (`taskDestination`'s `taskHref ?? folderHref ?? "/tasks"`), silently
   *  overwriting one task's completion with another's. A fresh `notify()`
   *  call that lands in the SAME family as an already-retained row UPDATES
   *  that row in place (same id, `count` incremented) instead of stacking a
   *  second, byte-identical one, for as long as that row is still sitting in
   *  the retained list undismissed — see `notify()`'s own comment on this. */
  family: string;
  /** How many times this family has fired while its row has sat retained —
   *  1 the first time, incremented on each collapse. `MessageRowView`
   *  (RepoUpdatesDock.tsx) reads this to show a repeat count once it exceeds
   *  1. */
  count: number;
  /** When this row's content was last set (fresh `notify()` or a collapsed
   *  repeat). Informational only — no longer gates the collapse window (see
   *  `notify()`'s own comment: collapse now lasts as long as the row is
   *  retained, not a fixed burst window). */
  updatedAt: number;
  /** A dimmed caption naming who raised this row — the message-side
   *  counterpart to `Job.origin` (jobs.ts, `.dl-origin`/`caption` on
   *  `NotificationCard`). Computed once at `notify()` time from `source` via
   *  `labelForSource` below; "" (never stored — see `toStored`) draws no
   *  element at all, same rule `Job.origin` follows. */
  origin?: string;
  // Dismissed, but still rendered while its exit animation plays (see
  // TOAST_EXIT_MS). Only ever true on the POPUP — a retained row is simply
  // removed outright, it has no exit animation of its own to play.
  leaving: boolean;
}

// How long a dismissed popup stays around so it can fade + collapse. Must
// match the .toast/.toast-slot exit transition in shell.css (--dur-med) —
// unchanged from toast.ts, and JobPopupCard already imports this constant
// from here (re-exported, not duplicated — see the spec's own instruction on
// this point).
export const TOAST_EXIT_MS = 150;

// How many retained (attention/trail) rows this store keeps at once — same
// number and same reasoning `toast.ts`'s MAX_TOASTS carried: a bound on the
// column's height, not a meaningful count. Transient/silent messages never
// reach this list at all, so this cap is only ever about what "Worth
// keeping"/"Needs you" can hold from client-raised messages.
export const MAX_RETAINED = 5;

// `IS_TOP_EMBED` proper (`router.ts:169`) is a `const` frozen once at that
// module's own init from `location`/`window` — correct for production (a
// document really cannot be re-parented mid-life), but untestable directly:
// bun shares one module registry across every file in a single `bun test`
// invocation (testDomShim.ts's own header comment), so a SECOND test file
// setting up an embed `location` before importing this module still gets
// `router.ts`'s value from whichever file happened to import it FIRST in
// that run. This override exists solely so `notifications.test.ts` can
// exercise the IS_TOP_EMBED branch without fighting that caching — it
// defaults to the real flag and every non-test caller never touches it.
let isTopEmbedOverride: boolean | null = null;
function effectiveIsTopEmbed(): boolean {
  return isTopEmbedOverride ?? IS_TOP_EMBED;
}
/** Test-only — see `effectiveIsTopEmbed`'s comment. `null` restores the real
 *  `IS_TOP_EMBED` reading. */
export function _setIsTopEmbedForTest(value: boolean | null): void {
  isTopEmbedOverride = value;
}

// Same override, same reason, for `IS_EMBED` — needed to exercise the
// pane→shell forwarding path (§4) without a second real module instance
// (see this module's own dead-end note in DECISIONS-toasts-become-
// notifications.md on why that approach doesn't work under bun's shared
// module registry).
let isEmbedOverride: boolean | null = null;
function effectiveIsEmbed(): boolean {
  return isEmbedOverride ?? IS_EMBED;
}
/** Test-only — see `effectiveIsEmbed`'s comment. `null` restores the real
 *  `IS_EMBED` reading. */
export function _setIsEmbedForTest(value: boolean | null): void {
  isEmbedOverride = value;
}

let popup: StoredNotification | null = null;
let retained: StoredNotification[] = [];
let nextId = 1;
// The popup's own exit timer, if one is running — at most one at a time,
// since there is only ever one popup.
let exitTimer: number | null = null;
const listeners = new Set<() => void>();

function emit(): void {
  for (const l of listeners) l();
}

function subscribe(cb: () => void): () => void {
  listeners.add(cb);
  return () => {
    listeners.delete(cb);
  };
}

// TIMERS THROUGH `globalThis`, not `window` — toast.ts's own rule, carried
// forward verbatim: this module is imported by plenty of non-DOM code paths,
// and a timer scheduled through `window` fired inside a bun test file with no
// DOM shim installed, which aborted a whole-repo `bun test` run BETWEEN
// files rather than failing the one test that owned it.
const setTimer = (fn: () => void, ms: number): number =>
  globalThis.setTimeout(fn, ms) as unknown as number;
const clearTimer = (id: number | null): void => {
  if (id !== null) globalThis.clearTimeout(id);
};

/** Which tier a message actually gets, given what its caller declared — the
 *  client-side equivalent of `jobs.ts`'s `effectiveTier`. `tone: "error"`
 *  ALWAYS promotes to "attention", the same way a job in `error`/`cancelled`
 *  state always reads "attention" regardless of its producer's declared
 *  tier: a failure is never lost to a tier choice, whether that choice was
 *  the tone-derived default or an explicit (wrong) override. Only once that
 *  is ruled out does an explicit `tier` win; absent both, `tone: "info"`
 *  defaults to "transient" and no tone/tier at all also defaults to
 *  "transient" (an ordinary, forgettable confirmation is the safe default,
 *  not a retained one). */
function resolveTier(input: NotificationInput): JobTier {
  if (input.tone === "error") return "attention";
  if (input.tier) return input.tier;
  return "transient";
}

// THE RETENTION RULE (narrowed — user: "don't keep this in the list. just
// show popup. anything non actionable or error doesn't belong in the list").
// Was `tier === "attention" || tier === "trail"`; a bare "destructive but
// successful" record (a completed move, a batch delete, "Freed X — deleted
// <model>") no longer earns a place by itself — see
// DECISIONS-toasts-become-notifications.md for the reversal writeup. ONE
// helper, used by every site that used to spell out the old two-tier check,
// so the three call sites below cannot drift apart:
//   - "attention" (every `tone: "error"` message, via resolveTier's
//     promotion) is always retained, action/page or not — a failure is
//     always worth finding again.
//   - anything else is retained only if it carries a destination
//     (`input.action` or `input.page`) — a non-error message with something
//     to click on is actionable and belongs in the panel.
//   - "silent" is never retained, even if it happens to carry an action —
//     a producer that declared silence gets silence, full stop.
function isRetained(input: NotificationInput, tier: JobTier): boolean {
  if (tier === "silent") return false;
  if (tier === "attention") return true;
  return Boolean(input.action || input.page);
}

// SUPPRESSION (SPEC-quiet-notifications.md §2a, D-A's "as far as each store
// honestly can" for a client-raised message: it lives only in the document
// that raised it, so it is suppressed only when THAT document is focused and
// its source is on screen). Deliberately mirrors `isRetained`'s own
// "attention, or carries a destination" shape rather than inventing a second,
// subtly different actionability test — an error or an actionable message is
// never suppressed, exactly as it is never left un-retained.
function isSuppressed(input: NotificationInput, tier: JobTier): boolean {
  if (!input.source) return false;
  if (tier === "attention") return false;
  if (input.action || input.page) return false;
  return isFocusedHere(input.source);
}

/** See `StoredNotification.family`'s own doc comment.
 *
 * `input.familyKey`, when the caller set one, wins outright over every axis
 * below — see its own doc comment on `NotificationInput` for why this is an
 * explicit per-caller opt-in (a finished-task notice) rather than a change to
 * everyone's default caption+title identity (an unrelated error and an
 * unrelated info message in the same folder must still stay two rows).
 *
 * DEFECT (2026-09-17, live repro): `page` was ALSO tried as the row
 * identity's other half, and that is wrong in the opposite direction from
 * the one the DEFECT-3 comment above used to warn about. `page` here is
 * `taskDestination(task)` -> `taskHref` (tasks-lib.ts), which for a task with
 * a live session embeds that run's own PER-RUN `session_id`
 * (`explorerUrl(task.target || task.project, task.session_id)`). Two
 * separate runs of the identical task — the user's own "i ran it twice, I
 * just want them grouped" case — therefore get two DIFFERENT `page` values
 * and never collapse, no matter how identical their title and caption are.
 * `page` is the wrong axis on both ends: too coarse when it falls back to a
 * shared folder/global route (DEFECT 3), too fine when it carries a
 * per-run session id (this fix).
 *
 * The axis that is actually stable across repeats of "the same work" is the
 * CAPTION — "who/what made this" (`input.origin || labelForSource(input.source)`,
 * exactly `toStored`'s own caption computation, kept in lockstep with it on
 * purpose) — plus the title. A caption is set for every call site this
 * collapse exists for (the finished-task notice sets `origin` to the task's
 * project caption; other retained callers set `source`), so this is tried
 * FIRST and, when it resolves to something non-empty, wins outright — page
 * is not consulted at all in that case, which is exactly what fixes the
 * repro (same caption, same title, different page -> same family).
 *
 * Only when there is no caption at all (a caller with neither `origin` nor
 * `source`) does this fall back to the pre-existing `page`-then-`title`
 * chain, so no existing call site's behavior changes: `page` alone is still
 * not a row identity for that fallback (DEFECT 3's reasoning stands — two
 * different captionless tasks sharing a folder-fallback page must not
 * collapse), and `title` alone remains the last resort for messages with no
 * destination and no caption at all. */
function messageFamily(input: NotificationInput): string {
  if (input.familyKey) return `familyKey:${input.familyKey}`;
  const caption = input.origin || labelForSource(input.source);
  if (caption) return `caption:${caption}::${input.title}`;
  return input.page ? `page:${input.page}::${input.title}` : `title:${input.title}`;
}

function toStored(input: NotificationInput, id: number): StoredNotification {
  return {
    id,
    title: input.title,
    detail: input.detail,
    tier: resolveTier(input),
    tone: input.tone,
    action: input.action,
    extraAction: input.extraAction,
    page: input.page,
    family: messageFamily(input),
    count: 1,
    updatedAt: Date.now(),
    // `input.origin` wins when the caller gave one explicitly — see its own
    // doc comment on `NotificationInput` for why this is a SEPARATE field
    // from `source` rather than the same one doing double duty. Falling back
    // to `labelForSource(input.source)` keeps every existing `source`-only
    // caller's caption unchanged.
    origin: input.origin || (labelForSource(input.source) || undefined),
    leaving: false,
  };
}

function capRetained(list: StoredNotification[]): StoredNotification[] {
  if (list.length <= MAX_RETAINED) return list;
  return list.slice(list.length - MAX_RETAINED);
}

// PANE → SHELL FORWARDING (§4 of the spec). A pane (IS_EMBED, not
// IS_TOP_EMBED) has no Notifications panel of its own (App.tsx's own
// `!IS_EMBED` guard around RepoUpdatesDock) — its retained rows would
// otherwise be created and then never seen by anyone. The established idiom
// for this shell, NOT `postMessage` (see main.tsx, ChatFrame.tsx and
// `apps/explorer/lib/snapshot-clear.ts`'s own comment on why): a plain global
// installed on a same-origin window, called directly, wrapped in try/catch
// for the cross-origin/sandboxed-frame SecurityError case. Direction here is
// child → parent, so every document installs the RECEIVING end on itself
// (harmless for a pane — nothing ever calls its own copy) and a pane calls
// the sending end on `window.top`.
//
// The ingest handler takes a plain `NotificationInput` — NOT a
// `StoredNotification` with the SENDER's own id baked in. Every document's
// `nextId` starts at 1, so forwarding a sender-minted id verbatim collided
// across documents (duplicate React keys in the shell's retained list, and
// `dismissNotification(1)` in one pane silently removing an unrelated row in
// another). The receiving document instead mints its OWN id from its OWN
// `nextId` sequence, exactly as if `notify()` had been called locally, and
// hands that id back to the caller so the pane can remember which shell-side
// id its own (locally-invisible) retained copy corresponds to.
//
// ROUTED THROUGH `retainAndCollapse()` — the SAME family-collapse lookup
// `notify()` itself calls — not a hand-rolled append and NOT `notify()`
// itself. This used to skip that lookup entirely and push straight onto
// `retained`, so N documents forwarding the SAME finished-task notice (e.g. N
// sub-documents watching one task) stacked N byte-identical rows instead of
// collapsing into one with `count` incremented (the live repro: three copies
// of one finished-task notice, two of another) — `retainAndCollapse` is what
// fixes that: family-collapse and the id it mints/returns now apply
// identically whether a message was raised here or forwarded in.
//
// NOT ROUTED THROUGH `notify()` (F1, 2026-09-18 fix, code review round): a
// pane already pops its OWN card, in the pane's own corner, for this exact
// message (App.tsx's `!IS_EMBED` guard means only a pane's own document runs
// `MessagePopupCard`'s upstream `notify()` call in the first place). If the
// shell's ingest handler also called `notify()`, the receiving document would
// pop a SECOND, identical card at the same moment — the exact double-popup
// `NotificationHost.tsx`'s own header comment already documents its
// `!IS_EMBED`-gating of `JobPopupCard`/`ServerStatusBanner` as guarding
// against, just for a different card. Worse, `notify()`'s "latest wins" popup
// swap (`clearTimer(exitTimer); popup = item`) would let a background pane's
// forwarded notice silently evict whatever card the SHELL itself was
// currently showing, cancelling that card's own exit timer. `ingestNotification`
// below therefore calls `retainAndCollapse` directly and stops there — the
// shell's retained list (and Notifications panel) still gets the row, but no
// popup card is ever armed for it. `isSuppressed` is still checked first
// (matching what a local `notify()` call would do), even though it can never
// actually fire here in practice: `forwardToShell` (below) forwards `origin`,
// never `source`, and `isSuppressed` short-circuits to `false` whenever
// `input.source` is absent.
//
// Re-forwarding is still structurally impossible in the ordinary
// (single-level pane -> top shell) case: `forwardToShell` only fires when
// THIS document is itself an embedded, non-top pane (its own guard, PLUS the
// F2 self-forward guard below), which the receiving (shell) document never is
// for a message it just received.
function ingestNotification(input: NotificationInput): number {
  if (isSuppressed(input, resolveTier(input))) return -1;
  const { id } = retainAndCollapse(input);
  refreshSnapshot();
  emit();
  return id;
}

function installIngest(): void {
  try {
    (globalThis as unknown as {
      _fusedIngestNotification?: (input: NotificationInput) => number;
      _fusedDismissNotification?: (id: number) => void;
    })._fusedIngestNotification = (input: NotificationInput) => ingestNotification(input);
    (globalThis as unknown as {
      _fusedDismissNotification?: (id: number) => void;
    })._fusedDismissNotification = (id: number) => {
      dismissNotification(id);
    };
  } catch {
    // Nothing sensible to do if this document's own globals can't be set.
  }
}
installIngest();

// local (pane-side) id -> shell-minted id, for every retained item this
// document has forwarded — lets `dismissNotification` reach across and
// remove the shell's own, independently-identified copy (finding #8): the
// pane's own `retained` entry is invisible (no panel renders it, per
// App.tsx's `!IS_EMBED` guard), so without this map a pane dismiss would
// only ever clear a row nobody could see, leaving the shell's visible row
// stuck forever.
const forwardedIds = new Map<number, number>();

function forwardToShell(n: StoredNotification): number | undefined {
  if (!effectiveIsEmbed() || effectiveIsTopEmbed()) return undefined; // top-level window: nothing to forward to
  // STRUCTURAL SELF-FORWARD GUARD (F2, 2026-09-18 fix): `IS_TOP_EMBED`
  // (router.ts) is `IS_EMBED && window === window.top && !IS_PREVIEW &&
  // !IS_SNAPSHOT` — so a TOP-LEVEL window loaded at an embed URL with
  // `_preview=1` or `snapshot=1` is `IS_EMBED` but NOT `IS_TOP_EMBED`, and the
  // guard above does not fire for it. For that document `window.top` IS the
  // document itself, so without this check: notify() -> forwardToShell() ->
  // its OWN `_fusedIngestNotification` -> notify() -> forwardToShell() -> ...
  // forever (bounded only by a swallowed stack-overflow `RangeError`, with
  // every unwound frame still doing its retain/pop work first). `window.top
  // === window` is the honest, cheap structural test for "there is nothing
  // above me to forward to" — independent of (and a superset of) the
  // IS_TOP_EMBED/IS_PREVIEW/IS_SNAPSHOT combination above, so it also covers
  // any future embed variant that reaches this function while still being
  // its own top.
  if (window.top === window) return undefined;
  try {
    const top = window.top as unknown as {
      _fusedIngestNotification?: (input: NotificationInput) => number;
    };
    const input: NotificationInput = {
      title: n.title,
      detail: n.detail,
      // `n.tier` reads `StoredNotification.tier` (still the full `JobTier`,
      // shared with jobs.ts) — in practice it can never actually be "trail"
      // here, since only `resolveTier` (fed a `ClientNotificationTier` input)
      // ever produces a client-side `StoredNotification`. This ternary is
      // the type-safe bridge back to `ClientNotificationTier`, not a
      // real runtime case.
      tier: n.tier === "trail" ? undefined : n.tier,
      tone: n.tone,
      action: n.action,
      extraAction: n.extraAction,
      page: n.page,
      // Forward the ALREADY-RESOLVED caption as `origin`, not `source` — the
      // pane's own `n.origin` is `toStored`'s output (a label, not a
      // suppression key), and the shell has no way to re-derive a
      // `labelForSource` input from it. Forwarding it as `origin` reproduces
      // the same caption on the shell's own copy without accidentally
      // opting that copy into suppression it never asked for.
      origin: n.origin,
    };
    return top?._fusedIngestNotification?.(input);
  } catch {
    // Cross-origin/sandboxed frame (snapshot-clear.ts's own guard) — nothing
    // this pane can tell the shell in that case either.
    return undefined;
  }
}

function forwardDismissToShell(shellId: number): void {
  try {
    const top = window.top as unknown as {
      _fusedDismissNotification?: (id: number) => void;
    };
    top?._fusedDismissNotification?.(shellId);
  } catch {
    // Same cross-origin/sandboxed-frame case as forwardToShell.
  }
}

/** Stable snapshot for `useSyncExternalStore` — the tuple reference only
 *  changes when the store mutates. */
function getSnapshot(): { popup: StoredNotification | null; retained: StoredNotification[] } {
  return snapshot;
}
let snapshot: { popup: StoredNotification | null; retained: StoredNotification[] } = {
  popup,
  retained,
};
function refreshSnapshot(): void {
  snapshot = { popup, retained };
}

// GROUPING/UPDATION (user: "better notification grouping/updation for same
// source") — the shared half of "raise a fresh message" that both `notify()`
// (a local call) and `ingestNotification()` (F1, 2026-09-18 fix — see its own
// comment) call identically: a fresh, retained-worthy message that shares its
// family (see `messageFamily`) with an ALREADY-RETAINED row updates that row
// IN PLACE (same id, `count` incremented) rather than stacking a second,
// byte-identical one. NO TIME WINDOW (2026-09-17 fix, replacing an earlier
// `GROUP_GAP_MS`-gated version): the user's own motivating case — two runs of
// the same Claude task, finished far more than two minutes apart — is the
// plain reading of "better notification grouping/updation for same source": a
// row the user has not dealt with yet gets updated, not duplicated, no matter
// how long it has been sitting there. As long as the earlier row is still in
// `retained` (i.e. undismissed), a repeat lands on it; once it is dismissed,
// the family is gone and the next repeat starts a fresh row.
//
// Deliberately does NOT touch `popup` — that is `notify()`'s own job alone
// (see F1's comment on `installIngest`/`ingestNotification` for why the two
// were split apart).
//
// `GROUP_GAP_MS`/jobs.ts is untouched — server-side job grouping still uses
// its own burst window, unaffected by this change.
function retainAndCollapse(input: NotificationInput): { id: number; item: StoredNotification } {
  const tier = resolveTier(input);
  const now = Date.now();
  const collapseIdx = isRetained(input, tier)
    ? retained.findIndex((n) => n.family === messageFamily(input))
    : -1;

  const id = collapseIdx !== -1 ? retained[collapseIdx].id : nextId++;
  const base = toStored(input, id);
  const item: StoredNotification =
    collapseIdx !== -1 ? { ...base, count: retained[collapseIdx].count + 1, updatedAt: now } : base;

  if (isRetained(input, item.tier)) {
    if (collapseIdx !== -1) {
      retained = retained.map((n, i) => (i === collapseIdx ? item : n));
      // Not re-forwarded (§4 pane->shell): the shell-side copy already
      // exists under this same id (`forwardedIds` already maps it) — a
      // collapse only changes this document's own content/count, not
      // whether a shell copy needs minting.
    } else {
      retained = capRetained([...retained, item]);
      const shellId = forwardToShell(item);
      if (shellId !== undefined) forwardedIds.set(id, shellId);
    }
  }

  return { id, item };
}

/** Queue a notification. Pops a card in `.notif-host` for
 *  `JOB_POPUP_VISIBLE_MS`, then (only if `isRetained` says so — an error, or
 *  a message carrying an action/page) stays in the Notifications panel until
 *  dismissed.
 *
 *  `replaceId`, exactly as `toast.ts`'s own `pushToast` — a repeated notice
 *  ("Still undoing…" on a second Cmd+Z) passes back the id it got last time
 *  so N repeats update one standing entry rather than popping N cards. If
 *  that id still names the live popup, or a still-retained row, it is
 *  updated in place and the same id comes back; otherwise a fresh id is
 *  minted exactly as if no id had been given. */
export function notify(input: NotificationInput, replaceId?: number): number {
  // Checked before anything pops or is retained, and before the replaceId
  // branch: a suppressed repeat must not resurrect (or keep alive) whatever
  // its earlier, unsuppressed call already popped.
  if (isSuppressed(input, resolveTier(input))) {
    if (replaceId !== undefined) {
      // The thing being "still going" is now suppressed too (its source came
      // into focus) — the same clear-not-leave-stale rule the popup's own
      // exit path follows elsewhere in this function.
      if (popup && popup.id === replaceId) dismissPopup(replaceId);
      retained = retained.filter((n) => n.id !== replaceId);
      refreshSnapshot();
    }
    return replaceId ?? -1;
  }
  if (replaceId !== undefined) {
    if (popup && popup.id === replaceId && !popup.leaving) {
      const updated = toStored(input, replaceId);
      popup = { ...updated, leaving: popup.leaving };
      // A retained row for the SAME id (finding #7a) must reflect the
      // update too — otherwise the panel keeps showing stale content the
      // live popup has already moved past. (A no-op if no such row exists.)
      retained = retained.map((n) => (n.id === replaceId ? updated : n));
      // Re-arm, not merely re-stamp: a caller that keeps replacing the SAME
      // id (a paste's "Copying N of M…", an undo's repeated "Still
      // undoing…") is saying "this is still going", and the card must not
      // silently vanish out from under a still-running operation just
      // because the FIRST call's 2.5s clock happened to run out. Skipped
      // only where notify()'s own fresh-item path below also skips it —
      // `silent` never pops, and an under-IS_TOP_EMBED `attention` message
      // never auto-expires.
      const neverExpiresHere = effectiveIsTopEmbed() && updated.tier === "attention";
      if (updated.tier !== "silent" && !neverExpiresHere) armExitTimer(JOB_POPUP_VISIBLE_MS);
      refreshSnapshot();
      emit();
      return replaceId;
    }
    const idx = retained.findIndex((n) => n.id === replaceId);
    if (idx !== -1) {
      const updated = toStored(input, replaceId);
      if (isRetained(input, updated.tier)) {
        retained = retained.map((n) => (n.id === replaceId ? updated : n));
        // This re-pops the entry as the live popup, replacing whatever was
        // showing before — always clear any timer that popup had armed for
        // ITSELF first (finding #6): left running, it fires against
        // whatever `popup` now IS (this updated entry), not the content it
        // was actually armed for, silently breaking the IS_TOP_EMBED
        // no-expiry guarantee (or, off that path, just firing early/late
        // against the wrong content).
        clearTimer(exitTimer);
        exitTimer = null;
        popup = updated;
        if (!(effectiveIsTopEmbed() && updated.tier === "attention")) {
          armExitTimer(JOB_POPUP_VISIBLE_MS);
        }
      } else {
        // Finding #7b: the new content no longer resolves to a retained
        // tier (e.g. an "attention" row updated into a plain transient
        // note) — it must not keep sitting in the retained list just
        // because that's where its old id happened to live.
        retained = retained.filter((n) => n.id !== replaceId);
      }
      refreshSnapshot();
      emit();
      return replaceId;
    }
  }

  const { id, item } = retainAndCollapse(input);

  // LATEST WINS: a fresh popup always replaces whatever is currently
  // showing — see this module's own header comment on why that differs
  // from toast.ts's old simultaneous stack. `silent` never pops at all
  // (mirrors jobs.ts: silence is only ever about a producer that has
  // nothing new to say), so it clears whatever WAS popping without
  // replacing it with anything.
  clearTimer(exitTimer);
  exitTimer = null;
  popup = item.tier === "silent" ? null : item;
  refreshSnapshot();
  emit();

  // IS_TOP_EMBED's own exception (SPEC §4): a standalone tab/bookmark has no
  // shell underneath it to retain an "attention" message for, so that one
  // tier, in that one context, never auto-expires — it sits until the user
  // dismisses it (MessagePopupCard's own outside-press/✕) or presses
  // elsewhere. Every other tier still times out normally even there; only
  // a failure would otherwise vanish with no history anywhere.
  const neverExpiresHere = effectiveIsTopEmbed() && item.tier === "attention";
  if (item.tier !== "silent" && !neverExpiresHere) armExitTimer(JOB_POPUP_VISIBLE_MS);

  return id;
}

// `JOB_POPUP_VISIBLE_MS` is jobs.ts's own constant (2500ms), imported at the
// top of this file rather than re-declared — the same "one constant, not two
// spellings of it" rule TOAST_EXIT_MS above follows for the reverse
// direction (jobs.ts's JobPopupCard importing FROM this module).
function armExitTimer(visibleMs: number): void {
  clearTimer(exitTimer);
  exitTimer = setTimer(() => {
    if (!popup) return;
    popup = { ...popup, leaving: true };
    refreshSnapshot();
    emit();
    exitTimer = setTimer(() => {
      popup = null;
      exitTimer = null;
      refreshSnapshot();
      emit();
    }, TOAST_EXIT_MS);
  }, visibleMs);
}

/** Remove a RETAINED row (a panel ✕, or "Clear all"). Never touches the
 *  popup — see `platform/ui/JobPopupCard.tsx`'s own rule, reused verbatim
 *  here: swatting the popup is "I saw this, stop showing it to me", not
 *  "delete the Notifications row", so the popup's own ✕ starts its exit
 *  animation directly rather than calling this. */
export function dismissNotification(id: number): void {
  const shellId = forwardedIds.get(id);
  if (shellId !== undefined) {
    forwardDismissToShell(shellId);
    forwardedIds.delete(id);
  }
  const next = retained.filter((n) => n.id !== id);
  if (next.length === retained.length) return; // already gone
  retained = next;
  refreshSnapshot();
  emit();
}

/** Close the POPUP only, immediately (its own exit animation still plays,
 *  same shape as `dismissToast`) — the retained row (if this message has
 *  one) is untouched. This is what the popup card's own ✕/outside-press
 *  calls; a caller that wants to clear the retained row too calls
 *  `dismissNotification` as well.
 *
 *  `id`, optional for the popup's own ✕ (which always means "whatever's
 *  showing right now"), is required for correctness anywhere else: a
 *  delayed caller (e.g. a "Reconnect" action on a retained row, fired
 *  whenever the user eventually clicks it) captured the id its own popup
 *  had when it first appeared, and by the time it runs an unrelated
 *  `notify()` has almost always already replaced the popup with something
 *  else — a bare `dismissPopup()` would close THAT unrelated card instead
 *  of doing nothing. Passing the id makes the call a no-op once it no
 *  longer names the current popup. */
export function dismissPopup(id?: number): void {
  if (!popup || popup.leaving) return;
  if (id !== undefined && popup.id !== id) return;
  clearTimer(exitTimer);
  popup = { ...popup, leaving: true };
  refreshSnapshot();
  emit();
  exitTimer = setTimer(() => {
    popup = null;
    exitTimer = null;
    refreshSnapshot();
    emit();
  }, TOAST_EXIT_MS);
}

/** Test-only reset — mirrors what `toast.test.ts` did by hand via
 *  dismiss-and-wait; exposed directly so tests don't need to fight the exit
 *  timers to get back to empty between cases. Not used by any non-test
 *  caller. */
export function _resetNotificationsForTest(): void {
  clearTimer(exitTimer);
  exitTimer = null;
  popup = null;
  retained = [];
  nextId = 1;
  forwardedIds.clear();
  refreshSnapshot();
  emit();
}

export function useNotificationPopup(): StoredNotification | null {
  return useSyncExternalStore(subscribe, () => getSnapshot().popup);
}

export function useRetainedNotifications(): StoredNotification[] {
  return useSyncExternalStore(subscribe, () => getSnapshot().retained);
}

/** Non-reactive reads for one-off checks (e.g. `useMissingFolders.ts`'s
 *  dedup against an already-retained message, and this module's own tests)
 *  — the equivalent of `toast.ts`'s exported `getToasts`. */
export function getRetainedNotifications(): StoredNotification[] {
  return retained;
}

export function getPopupNotification(): StoredNotification | null {
  return popup;
}
