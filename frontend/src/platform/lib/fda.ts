// Full Disk Access state — ONE store for every surface that talks about it.
//
// FdaStrip (Home, /apps, the explorer's AccessDenied card) and the onboarding
// FdaStep used to each poll /api/config on their own timer with their own
// idea of the state, so a grant could be "seen" by one and not another, and
// the copy drifted. This module owns the subscription, the shape, and the
// words; the components render.
//
// The server (fused_render/shell/fda.py) is the only source of truth: the
// `fda` field of /api/config, or its absence. Absent = not offered (non-mac,
// dev server) or inconclusive = render nothing.
//
// SINCE 2026-10-09 NOTHING HERE POLLS (D3). The state arrives over the
// document's events-bus socket (`fda`, platform/lib/events): the server
// pushes `{ fda }` — the `fda` field of GET /api/config, exactly — on
// subscribe and whenever it moves, at the old 3 s cadence while a grant is
// still possible. The subscription opens with the first `subscribeFda`
// listener and closes with the last; a listener joining is answered with a
// snapshot at once — AccessDenied's contract is that the server flipped
// `denied` inside the very request that failed, so the card's mount-time
// read already sees it. The "stop once granted / not offered" rule that
// bounded the timer is the server's now: it simply has nothing new to push.
import { useSyncExternalStore } from "react";

import type { FdaState } from "@platform/lib/api";
import { resyncTopic, subscribeTopic } from "@platform/lib/events";
import { bundleName, deepLinkScheme } from "@platform/lib/flavor";

export type { FdaState };

// undefined = not fetched yet; null = the server has no `fda` field.
export type FdaSnapshot = FdaState | null | undefined;

/** The `fda` topic's snapshot: the `fda` field of GET /api/config, absent
 *  when the server does not offer it. */
type FdaFrame = { fda?: FdaState | null };

const TOPIC = "fda";

// The one deep link that respawns the SAME version so a fresh grant takes
// effect (fused_render/deeplink.py). Rendered as a plain <a>, like the
// update dialog's Restart: the OS hands it to the running app, which quits
// through the normal teardown and respawns; this tab's subscription picks the
// new server up on its own (the client reconnects and resubscribes).
//
// A FUNCTION, not a constant: the scheme is the flavor's (`fused-bot://` for
// Fused Bot, platform/lib/flavor.ts), and the flavor is seeded after this
// module is evaluated — a constant here would bake in the default.
export function relaunchHref(): string {
  return `${deepLinkScheme()}://relaunch?reason=fda`;
}

// Shared copy, so the wizard step and the strip say the same thing. The pane
// lists the app by its BUNDLE name ("FusedRender" / "FusedBot", no space),
// so that is the word the steps use. A function for the same reason as the
// href above.
export function fdaCopy() {
  const app = bundleName();
  return {
    steps: [
      "Open System Settings on the Full Disk Access pane.",
      `Turn on ${app} in the list.`,
      `Relaunch ${app} — the grant applies to the next launch.`,
    ],
    waiting: `Waiting for the grant… turn ${app} on in the pane that just opened.`,
    pending: `Full Disk Access is granted. Relaunch ${app} to apply it.`,
    grantedToast: "Full Disk Access is on — no more prompts",
    deniedToast: `macOS denied ${app} access to a file — grant Full Disk Access to fix this`,
    open: "Open System Settings",
    reopen: "Open System Settings again",
    relaunch: `Relaunch ${app}`,
  } as const;
}

let snapshot: FdaSnapshot = undefined;
const listeners = new Set<() => void>();
/** This store's one subscription to `fda`, or null while nobody listens. */
let lane: (() => void) | null = null;

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

// One frame from the bus. A refusal (`meta.error`: the server down
// mid-relaunch, say) keeps the last snapshot rather than blanking it — the
// strip must not vanish and reappear while the app respawns.
function onFrame(frame: FdaFrame | null) {
  if (frame === null) return;
  set(frame.fda ?? null);
}

function open() {
  if (lane) return;
  lane = subscribeTopic<FdaFrame>(TOPIC, {}, onFrame);
}

function close() {
  if (!lane) return;
  const stop = lane;
  lane = null;
  stop();
}

// Ask the bus for a fresh snapshot now and hand back what the store holds.
// The resync is an event, never awaited: the fresh frame lands through
// `onFrame` like any other and wakes the listeners, so a caller that wants
// the answer reads the store — `useFda` — rather than this promise's value,
// which is whatever is held at the moment of asking.
export async function refresh(): Promise<FdaSnapshot> {
  resyncTopic(TOPIC, {});
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

// The lane opens with the first listener and closes with the last: N readers
// share one subscription and one snapshot.
export function subscribeFda(cb: () => void): () => void {
  listeners.add(cb);
  if (listeners.size === 1) open();
  return () => {
    listeners.delete(cb);
    if (listeners.size === 0) close();
  };
}

// Re-ask after a change that makes the answer worth a fresh look — e.g. a
// dismissal took `denied` down, or the Settings pane was just opened.
export function pokeFda() {
  void refresh();
}

export function useFda(): FdaSnapshot {
  return useSyncExternalStore(subscribeFda, getFda, getFda);
}

// Test seam.
export function _resetFdaStore() {
  close();
  snapshot = undefined;
  listeners.clear();
}
