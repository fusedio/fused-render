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
import type { NotificationCardAction } from "@platform/ui/NotificationCard";

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
  /** Click destination for the retained panel row (SPEC
   *  actionable-notifications' "every row goes somewhere"). Unused by the
   *  popup card. */
  page?: string;
}

export interface StoredNotification {
  id: number;
  title: string;
  detail?: string;
  tier: JobTier;
  tone?: "error" | "info";
  action?: NotificationCardAction;
  page?: string;
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

function toStored(input: NotificationInput, id: number): StoredNotification {
  return {
    id,
    title: input.title,
    detail: input.detail,
    tier: resolveTier(input),
    tone: input.tone,
    action: input.action,
    page: input.page,
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
function installIngest(): void {
  try {
    (globalThis as unknown as {
      _fusedIngestNotification?: (input: NotificationInput) => number;
      _fusedDismissNotification?: (id: number) => void;
    })._fusedIngestNotification = (input: NotificationInput) => {
      const id = nextId++;
      const item = toStored(input, id);
      retained = capRetained([...retained, item]);
      refreshSnapshot();
      emit();
      return id;
    };
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
      page: n.page,
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

  const id = nextId++;
  const item = toStored(input, id);

  if (isRetained(input, item.tier)) {
    retained = capRetained([...retained, item]);
    const shellId = forwardToShell(item);
    if (shellId !== undefined) forwardedIds.set(id, shellId);
  }

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
