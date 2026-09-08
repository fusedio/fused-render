// Global toast store — a queue of transient banners rendered by
// NotificationHost at the app root, so a toast shows regardless of which view
// raised it. THE toast surface: a plain module store subscribed via
// useSyncExternalStore: mutations (pushToast/dismissToast) update the module
// array and notify subscribers; the host re-reads on every change.
//
// No auto-dismiss: a toast stays until the user clears it or the code that
// raised it dismisses it explicitly. A message that vanished on its own clock
// could disappear before anyone read it, which is exactly what a toast
// carrying a failure must never do.
import { useSyncExternalStore } from "react";
import type { ToastAction, ToastTone } from "@platform/ui/Toast";

export type { ToastAction, ToastTone };

export interface ToastItem {
  id: number;
  msg: string;
  tone: ToastTone;
  action?: ToastAction;
  // Dismissed, but still rendered while its exit animation plays (see
  // TOAST_EXIT_MS). The host paints these with `.toast-leaving`; nothing else
  // should treat them as live.
  leaving: boolean;
}

// How long a dismissed toast stays in the queue so it can fade + collapse
// (which is also what makes the toasts below it glide up instead of snapping).
// Must match the .toast/.toast-slot exit transition in shell.css (--dur-med).
export const TOAST_EXIT_MS = 150;

// The stack holds at most this many toasts at once. A 6th arrival drops the
// oldest rather than growing the column without bound.
export const MAX_TOASTS = 5;

let toasts: ToastItem[] = [];
let nextId = 1;
// Exit timers, keyed by toast id. A toast in its exit window has no timer to
// cancel other than this one, and a dismiss landing mid-exit must not restart
// or shorten the animation.
const exiting = new Map<number, number>();
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

// Stable snapshot: the array reference only changes when the queue mutates, so
// useSyncExternalStore stays render-free between pushes/dismisses. Includes
// toasts in their exit window (`leaving: true`) — they are still on screen.
export function getToasts(): ToastItem[] {
  return toasts;
}

// Queue a toast. Stays until dismissed — by the user's ✕, by a caller
// dismissing it once its own action succeeds, or automatically once it falls
// off the back of the MAX_TOASTS stack. Returns the id so callers can dismiss
// it themselves.
//
// `replaceId`, when given, is a toast id from an earlier `pushToast` call —
// a repeated notice (e.g. "Still undoing…" on a second Cmd+Z while the first
// is still running) passes back the id it got last time instead of always
// omitting it, so N repeats update one standing toast rather than stacking
// N of them. If that id still names a live (non-leaving) toast, its
// message/tone/action are replaced in place and the same id comes back; if
// it has already left the stack (dismissed, or never existed), a new toast
// is pushed exactly as if no id had been given.
export function pushToast(
  t: { msg: string; tone: ToastTone; action?: ToastAction },
  replaceId?: number,
): number {
  if (replaceId !== undefined) {
    const existing = toasts.find((x) => x.id === replaceId && !x.leaving);
    if (existing) {
      toasts = toasts.map((x) =>
        x.id === replaceId ? { ...x, msg: t.msg, tone: t.tone, action: t.action } : x,
      );
      emit();
      return replaceId;
    }
  }
  const id = nextId++;
  toasts = [...toasts, { id, msg: t.msg, tone: t.tone, action: t.action, leaving: false }];
  // Cap the stack at MAX_TOASTS by dropping the oldest live (non-leaving)
  // entries first — a toast already animating out is not what "too many"
  // means, and forcing it straight to removed would skip its own exit.
  const live = toasts.filter((x) => !x.leaving);
  if (live.length > MAX_TOASTS) {
    const drop = new Set(live.slice(0, live.length - MAX_TOASTS).map((x) => x.id));
    toasts = toasts.filter((x) => !drop.has(x.id));
  }
  emit();
  return id;
}

// Dismiss = start the exit animation, not "remove". The toast keeps its slot in
// the queue (and therefore its place in the column) with `leaving: true` for
// TOAST_EXIT_MS, then goes. Both dismiss routes land here: the ✕, and a caller
// dismissing its own toast once its action has succeeded.
export function dismissToast(id: number): void {
  if (exiting.has(id)) return; // already animating out — don't restart it
  let found = false;
  const next = toasts.map((t) => {
    if (t.id !== id) return t;
    found = true;
    return { ...t, leaving: true };
  });
  if (!found) return; // already gone — stay render-free
  toasts = next;
  exiting.set(id, window.setTimeout(() => removeToast(id), TOAST_EXIT_MS));
  emit();
}

function removeToast(id: number): void {
  exiting.delete(id);
  const next = toasts.filter((t) => t.id !== id);
  if (next.length === toasts.length) return;
  toasts = next;
  emit();
}

export function useToasts(): ToastItem[] {
  return useSyncExternalStore(subscribe, getToasts);
}
