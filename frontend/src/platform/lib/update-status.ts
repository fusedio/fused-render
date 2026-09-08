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
const POLL_WARM_MS = 15_000;
const WARM_WINDOW_MS = 120_000;
const startedAt = Date.now();

let current: UpdateStatus | null = null;
const listeners = new Set<() => void>();
let started = false;
let timer: ReturnType<typeof setTimeout> | undefined;

// Every re-arm bumps this; a poll that was already in flight when the timer
// was cleared sees a stale generation on landing and arms nothing, so a poke
// mid-request cannot leave two self-rearming chains running (review, PR #1049).
let generation = 0;

function set(next: UpdateStatus | null): void {
  // By VALUE: getConfig() hands back a fresh object every tick, so an identity
  // check never held and every subscriber re-rendered on every poll.
  if (JSON.stringify(next) === JSON.stringify(current)) return;
  current = next;
  listeners.forEach((fn) => fn());
}

async function poll(): Promise<void> {
  const mine = generation;
  let next: UpdateStatus | null = current;
  try {
    const config = await getConfig();
    next = config.update ?? null;
    set(next);
  } catch {
    // Server down — ServerStatusBanner owns that story; keep last state.
  }
  if (mine !== generation) return;
  timer = setTimeout(poll, pollDelay(next));
}

// How long until the next look. Busy while an install runs; WARM while the
// packaged app has an updater but it has not answered yet ("idle"/"checking":
// the server's first manifest check lands ~1s after boot, and a 60s tick
// after that left the badge up to a minute late); the slow idle tick otherwise
// — including for an unpackaged dev run, where `update` is absent and there is
// nothing to be quick about.
export function pollDelay(status: UpdateStatus | null, sinceStartMs = Date.now() - startedAt): number {
  if (status?.state === "installing") return POLL_BUSY_MS;
  // WARM ONLY WHILE THE FIRST ANSWER IS PLAUSIBLY STILL COMING (bugbot, PR
  // #1049): "idle" is also the packaged app's resting state after a check that
  // found nothing, so warm-on-idle forever would never settle. The server's
  // first check now starts ~1s after boot, so the warm window is dominated by
  // how long the check itself takes (a manifest fetch over the network, seconds
  // rather than sub-second) — two minutes after this page started the cadence
  // goes back to the slow tick for good.
  const pending = status?.state === "checking" || status?.state === "idle";
  if (status && pending && sinceStartMs < WARM_WINDOW_MS) return POLL_WARM_MS;
  return POLL_IDLE_MS;
}

// Re-arm the poll now — called after an install kicks off so
// installing-progress shows within POLL_BUSY_MS instead of waiting out the
// idle interval.
export function pokeUpdateStatus(): void {
  clearTimeout(timer);
  generation += 1;
  void poll();
}

// Let a caller push a freshly-fetched status straight into the store (the
// install button's optimistic update) without waiting on the next poll tick.
export function setUpdateStatus(next: UpdateStatus | null): void {
  set(next);
  // A pushed status re-arms the timer at the cadence IT calls for: a status
  // that says "installing" must not sit on a 60s idle tick armed by the poll
  // that ran before the install began.
  if (started) {
    clearTimeout(timer);
    generation += 1;
    timer = setTimeout(poll, pollDelay(next));
  }
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
