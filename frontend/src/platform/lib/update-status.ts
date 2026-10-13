// Shared self-update state — one store, same pattern as sidebarstate.ts. Three
// surfaces read this (the expanded UpdateBadge row, the collapsed rail dot on
// Preferences, and the Settings popover's own row) and none of them should
// talk to the server on its own: one subscription to the `update` topic of the
// events bus (platform/lib/events), shared via useSyncExternalStore, is enough
// for all three to stay in sync.
//
// SINCE 2026-10-09 NOTHING HERE POLLS (D3). The store used to own a
// `/api/config` timer with four cadences (2 s hot after boot, 15 s warm while
// the first check was still coming, 2 s busy through an install, 60 s idle) —
// see UpdateBadge.tsx's header for why it had to be its own and not
// ServerStatusBanner's. The server now pushes `{ update }` (the `update` field
// of GET /api/config, exactly as that GET would answer) on subscribe and on
// every change the updater makes, and at the old busy cadence while an
// install runs — so the badge sees "installing" → "installed" at the same
// freshness, and an idle session costs no request at all.
import { useSyncExternalStore } from "react";

import { updateCheck, updateInstall, type UpdateStatus } from "@platform/lib/api";
import { resyncTopic, subscribeTopic } from "@platform/lib/events";

/** The `update` topic's snapshot: the `update` field of GET /api/config,
 *  null when this server has no updater (an unpackaged dev run). */
type UpdateFrame = { update?: UpdateStatus | null };

// Check-on-return (Akshil, 2026-09-09). The server's own loop checks every five
// minutes (common.CHECK_INTERVAL_S), which is the floor under a session left
// open — but a user who comes back to the app should learn about a release in
// the seconds after they return, not up to five minutes later on the next
// tick. Coming back to the front is the moment to ask, so focus/visibilitychange
// trigger one POST /api/update/check, gated by a 30-minute gap so cmd-tabbing
// between two windows is not a run of requests. This is a CHECK the server
// performs (a manifest fetch), not a read of state — the state itself arrives
// over the bus whenever it moves. The gap starts at store start, not at 0: a
// launch has just checked (the server's first check runs ~1s after boot), so
// the first return inside half an hour of opening the app has nothing to learn.
const RETURN_CHECK_GAP_MS = 30 * 60_000;

let current: UpdateStatus | null = null;
const listeners = new Set<() => void>();
let started = false;
/** This store's one subscription to the `update` topic, or null while nobody
 *  reads the store. */
let lane: (() => void) | null = null;
// When a check was last TRIGGERED from here — bumped only by the return
// trigger below and the manual check. A pushed snapshot does not touch it: a
// snapshot costs the CDN nothing and says nothing new about cadence, so
// letting it bump this would suppress every return check forever.
let lastCheckTriggerAt = Date.now();

function set(next: UpdateStatus | null): void {
  // By VALUE: every snapshot is a fresh object, so an identity check never
  // held and every subscriber re-rendered on every frame.
  if (JSON.stringify(next) === JSON.stringify(current)) return;
  current = next;
  listeners.forEach((fn) => fn());
}

// There is no `holdThroughCheck` here any more (2026-09-10, same day it landed).
// The server's five-minute tick used to report "checking" from every state for
// the seconds a fetch took, and this store briefly held a relevant status
// through it so the accordion would not blink — which then left an Update
// button on screen that install() refused for those same seconds (bugbot, PR
// #1097). The fix moved to the server: `check()` only says "checking" when it
// entered from "idle", and keeps "available"/"installed"/"error" on the wire
// while it re-checks (update/mac.py). Nothing is held here; the wire is true.
//
// Nor is there a stale-response guard any more (2026-09-22's `generation`,
// found chasing a CI-only flake in UpdateNotifier.test.tsx): the hazard was a
// `getConfig()` fetch started by one bun test file's component mount landing
// during a later file's test and overwriting `current`. A subscription's
// callback stops the moment it is unsubscribed — `resetUpdateStatusForTests`
// closes the lane — so nothing can land late.

// One frame from the bus. A refusal (`meta.error`: the server is down, or a
// GET /api/config that failed) keeps the last state — ServerStatusBanner owns
// that story.
function onFrame(snap: UpdateFrame | null): void {
  if (snap === null) return;
  set(snap.update ?? null);
}

function openLane(): void {
  if (lane) return;
  lane = subscribeTopic<UpdateFrame>("update", {}, onFrame);
}

function closeLane(): void {
  if (!lane) return;
  const stop = lane;
  lane = null;
  stop();
}

// Ask the bus for a fresh snapshot now — called after an install kicks off so
// installing-progress shows at once instead of waiting on the server's next
// push, and after a check whose answer may have moved what status() reads
// off disk.
export function pokeUpdateStatus(): void {
  resyncTopic("update", {});
}

// Let a caller push a freshly-fetched status straight into the store (the
// install button's optimistic update) without waiting on the next frame.
export function setUpdateStatus(next: UpdateStatus | null): void {
  set(next);
}

// Whether a return to the app should spend a manifest check. Pure so the three
// things that make this wrong — checking too eagerly, checking in a dev run
// that has no updater at all, and checking for a document that is not actually
// visible (a `focus` can fire on a hidden document) — are testable without a
// DOM.
export function shouldCheckOnReturn(
  lastAt: number,
  now: number,
  status: UpdateStatus | null,
  visible: boolean
): boolean {
  if (!visible) return false;
  // No updater here: an unpackaged dev run has no `update` in /api/config, and
  // POST /api/update/check 404s. Nothing to ask.
  if (status === null) return false;
  // ONLY WHEN THERE IS NOTHING TO LOSE (bugbot, PR #1078): a check flips the
  // server to "checking" for the length of the manifest fetch, during which
  // install() refuses and the badge hides. An update already found, running,
  // installed or failed is an answer — re-asking can only take it away for a
  // moment. Only "idle" (nothing found yet) is worth a fresh look.
  if (status.state !== "idle") return false;
  return now - lastAt >= RETURN_CHECK_GAP_MS;
}

// ---- the manual check (Akshil, 2026-09-10: "give a check for updates button
// -> where we have update available button") -------------------------------
//
// The same POST the return trigger sends, fired by a press on the badge's idle
// row. The response IS the answer — check() is synchronous on the server — so
// the caller can word the row off the result without waiting for a frame. Goes
// through the server's 60s floor like every other manual-ish check: a press
// inside the gap gets the answer the last fetch left, at most a minute old,
// which is exactly what "up to date" meant a moment ago. Bumps the return
// trigger's clock too: a person who just pressed the button has nothing to
// learn from cmd-tabbing back in thirty seconds later.
export async function checkForUpdates(): Promise<UpdateStatus> {
  lastCheckTriggerAt = Date.now();
  const result = await updateCheck();
  setUpdateStatus(result);
  pokeUpdateStatus();
  return result;
}

/** How long the row holds "Up to date" / "Couldn't check" before it reads
 *  "Check for updates" again — long enough to be read, short enough that the
 *  slot never looks stuck on an old answer. */
export const CHECK_RESULT_HOLD_MS = 4_000;

/** The idle row's phases: resting, in flight, and the two answers that are not
 *  an update (an update found is not a phase — the store flips to "available"
 *  and the accordion takes the slot). */
export type ManualCheckPhase = "rest" | "checking" | "current" | "failed";

/** What the idle row says in each phase. Pure so the wording is tested once,
 *  next to updateLabel's, rather than read off a rendered tree. */
export function checkNowLabel(phase: ManualCheckPhase, version: string | null | undefined): string {
  if (phase === "checking") return "Checking…";
  if (phase === "current") return `Up to date${version ? ` · v${version}` : ""}`;
  if (phase === "failed") return "Couldn't check";
  return "Check for updates";
}

// The app came back to the front. Never throws: this runs off a window event
// with no caller to catch anything, and a failed check is exactly as
// uninteresting as a refused frame — the next one will do.
async function onReturn(): Promise<void> {
  const visible = document.visibilityState === "visible";
  if (!shouldCheckOnReturn(lastCheckTriggerAt, Date.now(), current, visible)) return;
  lastCheckTriggerAt = Date.now();
  try {
    const result = await updateCheck();
    setUpdateStatus(result);
    // The check itself is synchronous on the server, so `result` is already
    // the answer; the poke is for what follows it — the disk re-read status()
    // does when the bus builds its next snapshot.
    pokeUpdateStatus();
  } catch {
    // 404 (no updater), offline, server down — all of it is the bus's story.
  }
}

function ensureStarted(): void {
  if (started) return;
  started = true;
  // Registered once for the life of the page, alongside the one shared
  // subscription: three surfaces subscribe to this store and none of them
  // should own a listener. `focus` catches the app being brought forward, and
  // `visibilitychange` catches a tab/window that was hidden becoming visible
  // without a focus event of its own.
  lastCheckTriggerAt = Date.now();
  window.addEventListener("focus", () => void onReturn());
  document.addEventListener("visibilitychange", () => void onReturn());
}

/** Tests only: put the module back to its never-started state. The store is
 *  module-global by design (one subscription for three surfaces), which is
 *  exactly what lets one test's "available" leak into the next test's "nothing
 *  here". Closes the lane too, so a finished test file leaves no subscription
 *  behind whose frames could land in the next file's tests. */
export function resetUpdateStatusForTests(): void {
  closeLane();
  current = null;
  started = false;
  listeners.clear();
}

// The lane opens with the first reader and closes with the last: N readers
// share one subscription and one snapshot (the client refcounts the key too,
// but the store's own count is what lets the lane close when nobody looks).
function subscribe(fn: () => void): () => void {
  ensureStarted();
  listeners.add(fn);
  if (listeners.size === 1) openLane();
  return () => {
    listeners.delete(fn);
    if (listeners.size === 0) closeLane();
  };
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
  // No version in the label (it was always ellipsised in the status chip); the
  // chip's tooltip and the notification's detail carry it instead.
  return "Update available";
}

// The Download action's body, shared by the notification card and the sidebar
// chip so both surfaces take one path. The server force-rechecks the manifest
// before it installs and always installs the NEWEST version it finds — the one
// on screen, or a newer one published since. It never installs anything older
// than what was shown.
export async function installUpdate(status: UpdateStatus): Promise<void> {
  try {
    setUpdateStatus(await updateInstall(status.latest_version));
  } catch {
    // Fall through — the resync below picks up the real state.
  }
  pokeUpdateStatus();
}
