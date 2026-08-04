// Global toast store — a queue of transient banners rendered by
// NotificationHost at the app root, so a toast shows regardless of which view
// raised it. THE toast surface: Listing and Preview used to keep their own
// pane-local slot, which meant a second copy of the dismiss timer and the same
// message appearing in a different place depending on the view. A plain module
// store subscribed
// via useSyncExternalStore: mutations (pushToast/dismissToast) update the
// module array and notify subscribers; the host re-reads on every change.
//
// Auto-dismiss mirrors Listing's ~6s cadence. A persistent toast (ttlMs=0) —
// used for an error carrying an action the user must act on — stays until it's
// dismissed, either by the user or by the code that raised it.
import { useSyncExternalStore } from "react";
import type { ToastAction, ToastTone } from "../components/Toast";

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

const DEFAULT_TTL_MS = 6000;

// How long a dismissed toast stays in the queue so it can fade + collapse
// (which is also what makes the toasts below it glide up instead of snapping).
// Must match the .toast/.toast-slot exit transition in shell.css (--dur-med).
export const TOAST_EXIT_MS = 150;

let toasts: ToastItem[] = [];
let nextId = 1;
const timers = new Map<number, number>();
// Exit timers, keyed the same way. Separate from `timers`: a toast in its exit
// window has no TTL left to cancel, and a dismiss landing mid-exit must not
// restart or shorten the animation.
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

// Queue a toast. ttlMs defaults to ~6s; pass 0 to keep it up until dismissed
// (the reconnect-failed error, which carries a manual action). Returns the id
// so callers can dismiss it themselves (e.g. after the action succeeds).
export function pushToast(t: {
  msg: string;
  tone: ToastTone;
  action?: ToastAction;
  ttlMs?: number;
}): number {
  const id = nextId++;
  toasts = [...toasts, { id, msg: t.msg, tone: t.tone, action: t.action, leaving: false }];
  const ttl = t.ttlMs ?? DEFAULT_TTL_MS;
  if (ttl > 0) {
    timers.set(id, window.setTimeout(() => dismissToast(id), ttl));
  }
  emit();
  return id;
}

// Dismiss = start the exit animation, not "remove". The toast keeps its slot in
// the queue (and therefore its place in the column) with `leaving: true` for
// TOAST_EXIT_MS, then goes. Both dismiss routes land here: the TTL timer above
// calls this, and so does the ✕ / a caller dismissing its own toast.
export function dismissToast(id: number): void {
  const timer = timers.get(id);
  if (timer !== undefined) {
    window.clearTimeout(timer);
    timers.delete(id);
  }
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
