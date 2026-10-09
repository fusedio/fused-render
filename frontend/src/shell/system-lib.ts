// The System chip's view of `GET /api/system/activity` (shell/SystemDock.tsx),
// one shared snapshot however many components read it.
//
// SINCE 2026-10-09 NOTHING HERE POLLS (D3). The snapshot arrives over the
// document's events-bus socket (`system.activity` with `{ scope: "fused" }`,
// platform/lib/events): the server pushes the GET's body on subscribe and on
// every sampler tick after, for as long as anyone is subscribed — so the chip
// sees each reading the moment it exists, with no cold-start retries, no
// "busy process keeps it fast" rule and no slow tick to wait out (the 1 s /
// 30 s cadence, its cold retries and the visibility re-read all lived here).
// The sampler itself still runs only while it is being read: the subscription
// is the read, and it closes with the last reader. A hidden document's
// subscription is dropped by the client and resubscribed on return; the
// snapshot that answers is the catch-up.
import { useSyncExternalStore } from "react";
import { subscribeTopic } from "@platform/lib/events";
import type { SystemActivity } from "@platform/lib/sysmon";

export { formatBytes, formatCpu } from "@platform/lib/sysmon";

const TOPIC = "system.activity";
const PARAMS = { scope: "fused" } as const;

let snapshot: SystemActivity | null = null;
/** This module's one subscription, or null while nobody reads the chip. */
let lane: (() => void) | null = null;
const listeners = new Set<() => void>();

// One frame from the bus. A refusal (`meta.error`) keeps the last snapshot —
// best effort, as the failed read was.
function onFrame(data: SystemActivity | null): void {
  if (data === null) return;
  snapshot = data;
  listeners.forEach((l) => l());
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  if (listeners.size === 1 && !lane) lane = subscribeTopic<SystemActivity>(TOPIC, PARAMS, onFrame);
  return () => {
    listeners.delete(listener);
    if (!listeners.size && lane) {
      const stop = lane;
      lane = null;
      stop();
    }
  };
}

const getSnapshot = () => snapshot;

/** The latest `/api/system/activity` payload (null before the first read).
 *  `fast` used to ask for the 1 s cadence while the popover was open; the
 *  server now pushes every sampler tick to every subscriber, so there is no
 *  cadence left to request. The parameter stays for the callers' sake. */
export function useSystemActivity(fast = false): SystemActivity | null {
  void fast;
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
}

/** Test-only: forget the shared snapshot and close the subscription. */
export function resetSystemActivityForTests(): void {
  if (lane) {
    const stop = lane;
    lane = null;
    stop();
  }
  snapshot = null;
  listeners.clear();
}
