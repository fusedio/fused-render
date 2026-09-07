// Shared self-update poll — one store, same pattern as sidebarstate.ts. Three
// surfaces read this (the expanded UpdateBadge row, the collapsed rail dot on
// Preferences, and the Settings popover's own row) and none of them should run
// its own timer: update state changes rarely, so one poll shared via
// useSyncExternalStore is enough for all three to stay in sync.
//
// Owns its own slow poll (60s idle, 2s while installing) instead of riding
// ServerStatusBanner's 5s one — see UpdateBadge.tsx's header for why.
import { useSyncExternalStore } from "react";

import { getConfig, type UpdateStatus } from "@platform/lib/api";

const POLL_IDLE_MS = 60_000;
const POLL_BUSY_MS = 2_000;

let current: UpdateStatus | null = null;
const listeners = new Set<() => void>();
let started = false;
let timer: ReturnType<typeof setTimeout> | undefined;

function set(next: UpdateStatus | null): void {
  if (next === current) return;
  current = next;
  listeners.forEach((fn) => fn());
}

async function poll(): Promise<void> {
  let next: UpdateStatus | null = current;
  try {
    const config = await getConfig();
    next = config.update ?? null;
    set(next);
  } catch {
    // Server down — ServerStatusBanner owns that story; keep last state.
  }
  const busy = next?.state === "installing";
  timer = setTimeout(poll, busy ? POLL_BUSY_MS : POLL_IDLE_MS);
}

// Re-arm the poll now — called after an install kicks off so
// installing-progress shows within POLL_BUSY_MS instead of waiting out the
// idle interval.
export function pokeUpdateStatus(): void {
  clearTimeout(timer);
  poll();
}

// Let a caller push a freshly-fetched status straight into the store (the
// install button's optimistic update) without waiting on the next poll tick.
export function setUpdateStatus(next: UpdateStatus | null): void {
  set(next);
}

function ensureStarted(): void {
  if (started) return;
  started = true;
  poll();
}

function subscribe(fn: () => void): () => void {
  ensureStarted();
  listeners.add(fn);
  return () => listeners.delete(fn);
}

function getSnapshot(): UpdateStatus | null {
  return current;
}

export function useUpdateStatus(): UpdateStatus | null {
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
}

// Whether this status is worth showing anywhere — badge, rail dot, or popover
// row all gate on the same set of states.
export function updateRelevant(status: UpdateStatus | null): boolean {
  if (!status) return false;
  return (
    status.state === "available" ||
    status.state === "installing" ||
    status.state === "installed" ||
    status.state === "error"
  );
}

// Shared label text — the badge row, the popover row, and the rail dot's
// tooltip all say the same sentence about the same state.
export function updateLabel(status: UpdateStatus): string {
  if (status.state === "installing") return "Updating…";
  if (status.state === "installed") return "Ready to restart";
  return `Update available${status.latest_version ? ` — v${status.latest_version}` : ""}`;
}
