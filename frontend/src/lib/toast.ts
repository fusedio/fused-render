// Global toast store — a queue of transient banners rendered by ToastHost at
// the app root, so a toast shows regardless of which view is mounted (unlike
// Listing's own local, listing-scoped toast). A plain module store subscribed
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
}

const DEFAULT_TTL_MS = 6000;

let toasts: ToastItem[] = [];
let nextId = 1;
const timers = new Map<number, number>();
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
// useSyncExternalStore stays render-free between pushes/dismisses.
function getSnapshot(): ToastItem[] {
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
  toasts = [...toasts, { id, msg: t.msg, tone: t.tone, action: t.action }];
  const ttl = t.ttlMs ?? DEFAULT_TTL_MS;
  if (ttl > 0) {
    timers.set(id, window.setTimeout(() => dismissToast(id), ttl));
  }
  emit();
  return id;
}

export function dismissToast(id: number): void {
  const timer = timers.get(id);
  if (timer !== undefined) {
    window.clearTimeout(timer);
    timers.delete(id);
  }
  const next = toasts.filter((t) => t.id !== id);
  if (next.length === toasts.length) return; // already gone — stay render-free
  toasts = next;
  emit();
}

export function useToasts(): ToastItem[] {
  return useSyncExternalStore(subscribe, getSnapshot);
}
