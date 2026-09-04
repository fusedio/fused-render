// Full Disk Access state — ONE store for every surface that talks about it.
//
// FdaStrip (Home, /apps, the explorer's AccessDenied card) and the onboarding
// FdaStep used to each poll /api/config on their own timer with their own
// idea of the state, so a grant could be "seen" by one and not another, and
// the copy drifted. This module owns the poll, the shape, and the words; the
// components render.
//
// The server (fused_render/shell/fda.py) is the only source of truth: the
// `fda` field of /api/config, or its absence. Absent = not offered (non-mac,
// dev server) or inconclusive = render nothing, and this store stops polling.
//
// The poll runs only while someone is subscribed and the answer can still
// change: `granted` is final for this process (a revoke needs a relaunch too),
// so polling stops there as well. A subscriber joining kicks an immediate
// fetch — AccessDenied's contract is that the server flipped `denied` inside
// the very request that failed, so the card's mount-time read must already
// see it, not wait for the next tick.
import { useSyncExternalStore } from "react";

import { getConfig, type FdaState } from "@platform/lib/api";

export type { FdaState };

// undefined = not fetched yet; null = the server has no `fda` field.
export type FdaSnapshot = FdaState | null | undefined;

//: While anyone is watching and the answer can still change. Localhost, and
//: the server memoizes the one expensive part (the child probe), so one
//: cadence for every stage is fine.
export const POLL_MS = 3000;

// The one deep link that respawns the SAME version so a fresh grant takes
// effect (fused_render/deeplink.py). Rendered as a plain <a>, like the
// update-restart banner: the OS hands it to the running app, which quits
// through the normal teardown and respawns; this tab's poll picks the new
// server up on its own.
export const RELAUNCH_HREF = "fused-render://relaunch?reason=fda";

// Shared copy, so the wizard step and the strip say the same thing.
export const FDA_COPY = {
  steps: [
    "Open System Settings on the Full Disk Access pane.",
    "Turn on FusedRender in the list.",
    "Relaunch FusedRender — the grant applies to the next launch.",
  ],
  waiting: "Waiting for the grant… turn FusedRender on in the pane that just opened.",
  pending: "Full Disk Access is granted. Relaunch FusedRender to apply it.",
  grantedToast: "Full Disk Access is on — no more prompts",
  deniedToast: "macOS denied FusedRender access to a file — grant Full Disk Access to fix this",
  open: "Open System Settings",
  reopen: "Open System Settings again",
  relaunch: "Relaunch FusedRender",
} as const;

let snapshot: FdaSnapshot = undefined;
const listeners = new Set<() => void>();
let timer: number | null = null;
let inflight = false;

function emit() {
  for (const l of listeners) l();
}

function set(next: FdaSnapshot) {
  const prev = snapshot;
  const same =
    prev === next ||
    (prev != null &&
      next != null &&
      prev.granted === next.granted &&
      prev.pending_relaunch === next.pending_relaunch &&
      prev.denied === next.denied);
  if (same) return;
  snapshot = next;
  emit();
}

function shouldPoll(): boolean {
  if (listeners.size === 0) return false;
  if (snapshot === null) return false; // not offered: nothing will change
  if (snapshot?.granted) return false; // final for this process
  return true;
}

function schedule() {
  if (timer !== null) return;
  if (!shouldPoll()) return;
  timer = window.setTimeout(() => {
    timer = null;
    void refresh().finally(schedule);
  }, POLL_MS);
}

function stop() {
  if (timer !== null) {
    window.clearTimeout(timer);
    timer = null;
  }
}

// Re-read the server now. Returns the fresh snapshot. A failed fetch (server
// down mid-relaunch, say) keeps the last snapshot rather than blanking it —
// the strip must not vanish and reappear while the app respawns.
export async function refresh(): Promise<FdaSnapshot> {
  if (inflight) return snapshot;
  inflight = true;
  try {
    const config = await getConfig();
    set(config.fda ?? null);
  } catch {
    // keep what we have
  } finally {
    inflight = false;
  }
  return snapshot;
}

// Seed from a config the caller already holds (the wizard is handed one), so
// the first paint does not wait on a round trip.
export function seedFda(fda: FdaState | undefined) {
  if (snapshot === undefined) set(fda ?? null);
}

export function getFda(): FdaSnapshot {
  return snapshot;
}

export function subscribeFda(cb: () => void): () => void {
  listeners.add(cb);
  if (listeners.size === 1) {
    void refresh().finally(schedule);
  } else {
    schedule();
  }
  return () => {
    listeners.delete(cb);
    if (listeners.size === 0) stop();
  };
}

// Restart the poll after a change that makes it worth watching again — e.g.
// a dismissal took `denied` down, or the Settings pane was just opened.
export function pokeFda() {
  void refresh().finally(schedule);
}

export function useFda(): FdaSnapshot {
  return useSyncExternalStore(subscribeFda, getFda, getFda);
}

// Test seam.
export function _resetFdaStore() {
  stop();
  snapshot = undefined;
  inflight = false;
  listeners.clear();
}
