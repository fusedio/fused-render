// Global notification store — replaces `lib/toast`. A client-raised message
// (a failure, a completed op) now shares the exact pop-then-retain lifetime
// the job system already has (`JobTier`, this module's own import): it pops a
// card in `.notif-host` for `JOB_POPUP_VISIBLE_MS`, then either leaves for
// good (`transient`, `silent`) or is retained in the Notifications panel
// (`attention`/`trail`) until the user dismisses it — see
// SPEC-toasts-become-notifications.md for the full reasoning and
// DECISIONS-toasts-become-notifications.md for the call-by-call tier table.
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
// THE RETAINED LIST is the part with no toast.ts precedent: `attention`/
// `trail` messages stay here — capped, like the old stack, at MAX_RETAINED —
// until `dismissNotification` (or the panel's "Clear all") removes them.
// `transient`/`silent` messages are never added to it; their popup is their
// only trace, same as the affected-flows diagram in the spec.
import { useSyncExternalStore } from "react";
import { JOB_POPUP_VISIBLE_MS } from "@platform/lib/jobs";
import type { JobTier } from "@platform/lib/jobs";
import { IS_EMBED, IS_TOP_EMBED } from "@platform/lib/router";
import type { NotificationCardAction } from "@platform/ui/NotificationCard";

export interface NotificationInput {
  title: string;
  detail?: string;
  /** An explicit tier wins over the tone-derived default — UNLESS `tone` is
   *  "error", which always promotes to "attention" regardless (see
   *  `resolveTier` below; mirrors `jobs.ts`'s `effectiveTier` promoting any
   *  error/cancelled JOB to "attention" no matter what its producer
   *  declared). */
  tier?: JobTier;
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
function installIngest(): void {
  try {
    (globalThis as unknown as {
      _fusedIngestNotification?: (n: StoredNotification) => void;
    })._fusedIngestNotification = (n: StoredNotification) => {
      retained = capRetained([...retained, n]);
      emit();
    };
  } catch {
    // Nothing sensible to do if this document's own global can't be set.
  }
}
installIngest();

function forwardToShell(n: StoredNotification): void {
  if (!IS_EMBED || IS_TOP_EMBED) return; // top-level window: nothing to forward to
  try {
    const top = window.top as unknown as {
      _fusedIngestNotification?: (n: StoredNotification) => void;
    };
    top?._fusedIngestNotification?.(n);
  } catch {
    // Cross-origin/sandboxed frame (snapshot-clear.ts's own guard) — nothing
    // this pane can tell the shell in that case either.
  }
}

/** Stable snapshot for `useSyncExternalStore` — the tuple reference only
 *  changes when the store mutates. */
function getSnapshot(): { popup: StoredNotification | null; retained: StoredNotification[] } {
  return snapshot;
}
let snapshot = { popup, retained };
function refreshSnapshot(): void {
  snapshot = { popup, retained };
}

/** Queue a notification. Pops a card in `.notif-host` for
 *  `JOB_POPUP_VISIBLE_MS`, then (for "attention"/"trail" only) stays in the
 *  Notifications panel until dismissed.
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
      refreshSnapshot();
      emit();
      return replaceId;
    }
    const idx = retained.findIndex((n) => n.id === replaceId);
    if (idx !== -1) {
      const updated = toStored(input, replaceId);
      retained = retained.map((n) => (n.id === replaceId ? updated : n));
      if (updated.tier === "attention" || updated.tier === "trail") {
        popup = updated;
        armExitTimer(JOB_POPUP_VISIBLE_MS);
      }
      refreshSnapshot();
      emit();
      return replaceId;
    }
  }

  const id = nextId++;
  const item = toStored(input, id);

  if (item.tier === "attention" || item.tier === "trail") {
    retained = capRetained([...retained, item]);
    forwardToShell(item);
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

  if (item.tier !== "silent") armExitTimer(JOB_POPUP_VISIBLE_MS);

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
 *  `dismissNotification` as well. */
export function dismissPopup(): void {
  if (!popup || popup.leaving) return;
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
