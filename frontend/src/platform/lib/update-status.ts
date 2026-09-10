// Shared self-update poll — one store, same pattern as sidebarstate.ts. Three
// surfaces read this (the expanded UpdateBadge row, the collapsed rail dot on
// Preferences, and the Settings popover's own row) and none of them should run
// its own timer: update state changes rarely, so one poll shared via
// useSyncExternalStore is enough for all three to stay in sync.
//
// Owns its own slow poll (60s idle, 2s while installing) instead of riding
// ServerStatusBanner's 5s one — see UpdateBadge.tsx's header for why.
import { useSyncExternalStore } from "react";

import { getConfig, updateCheck, type UpdateStatus } from "@platform/lib/api";

const POLL_IDLE_MS = 60_000;
const POLL_BUSY_MS = 2_000;
const POLL_WARM_MS = 15_000;
// HOT for the first moments after this page starts: the server's first check
// lands ~1s after boot, and a 15s tick from t≈0 put the badge at ~15s
// (Akshil, 2026-09-09: "the first check after boot took 14s"). Two-second
// ticks for the first twenty seconds catch it within a couple of seconds.
const POLL_HOT_MS = 2_000;
const HOT_WINDOW_MS = 20_000;
const WARM_WINDOW_MS = 120_000;
const startedAt = Date.now();
// Check-on-return (Akshil, 2026-09-09). The server's own loop checks every five
// minutes (common.CHECK_INTERVAL_S), which is the floor under a session left
// open — but a user who comes back to the app should learn about a release in
// the seconds after they return, not up to five minutes later on the next tick. Coming back to the front is the moment to ask,
// so focus/visibilitychange trigger one POST /api/update/check, gated by a
// 30-minute gap so cmd-tabbing between two windows is not a run of requests.
// The gap starts at store start, not at 0: a launch has just checked (the
// server's first check runs ~1s after boot), so the first return inside half
// an hour of opening the app has nothing to learn.
const RETURN_CHECK_GAP_MS = 30 * 60_000;

let current: UpdateStatus | null = null;
const listeners = new Set<() => void>();
let started = false;
let timer: ReturnType<typeof setTimeout> | undefined;
// When a check was last TRIGGERED from here — bumped only by the return
// trigger below. The 60s poll does not touch it: that poll only reads
// /api/config, which costs the CDN nothing and says nothing new about
// cadence, so letting it bump this would suppress every return check forever.
let lastCheckTriggerAt = Date.now();

// Every re-arm bumps this; a poll that was already in flight when the timer
// was cleared sees a stale generation on landing and arms nothing, so a poke
// mid-request cannot leave two self-rearming chains running (review, PR #1049).
let generation = 0;

function set(next: UpdateStatus | null): void {
  next = holdThroughCheck(current, next);
  // By VALUE: getConfig() hands back a fresh object every tick, so an identity
  // check never held and every subscriber re-rendered on every poll.
  if (JSON.stringify(next) === JSON.stringify(current)) return;
  current = next;
  listeners.forEach((fn) => fn());
}

/**
 * A check in flight does not un-know an update (bugbot, PR #1097). The server's
 * auto tick runs `check(force=True)` from EVERY state, and for the seconds the
 * manifest fetch takes it reports "checking" — which `updateRelevant` rejects,
 * so a poll landing mid-fetch swapped the "Update available" accordion for the
 * idle "Check for updates" row and back. At an hourly tick that was a rare
 * blink; at five minutes it is a habit. So a relevant status is HELD while the
 * next one says "checking": the fetch ends in seconds with the durable answer
 * (available / installed / idle), and that one is taken as usual. Pure, so the
 * rule is testable; `set` is the one place it is applied.
 */
export function holdThroughCheck(
  shown: UpdateStatus | null,
  next: UpdateStatus | null
): UpdateStatus | null {
  if (next?.state === "checking" && shown && updateRelevant(shown)) return shown;
  return next;
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
  // "checking" IS busy, at any age (bugbot, PR #1097): a manifest fetch lasts
  // seconds (FETCH_TIMEOUT_S bounds it at 15), and the server's own tick starts
  // one every five minutes, so the state is short-lived and frequent. Polled
  // at the slow tick, a manual check that landed mid-fetch — the server hands
  // back "checking" rather than an answer — would leave the row saying
  // "Checking…" for up to a minute after the answer existed. Bounded: the
  // busy cadence lasts exactly as long as the fetch does.
  if (status?.state === "checking") return POLL_BUSY_MS;
  // WARM ONLY WHILE THE FIRST ANSWER IS PLAUSIBLY STILL COMING (bugbot, PR
  // #1049): "idle" is also the packaged app's resting state after a check that
  // found nothing, so warm-on-idle forever would never settle. The server's
  // first check now starts ~1s after boot, so the warm window is dominated by
  // how long the check itself takes (a manifest fetch over the network, seconds
  // rather than sub-second) — two minutes after this page started the cadence
  // goes back to the slow tick for good.
  const pending = status?.state === "idle";
  if (status && pending && sinceStartMs < HOT_WINDOW_MS) return POLL_HOT_MS;
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
// the caller can word the row off the result without waiting for the poll. Goes
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
// uninteresting as a failed poll — the next one will do.
async function onReturn(): Promise<void> {
  const visible = document.visibilityState === "visible";
  if (!shouldCheckOnReturn(lastCheckTriggerAt, Date.now(), current, visible)) return;
  lastCheckTriggerAt = Date.now();
  try {
    const result = await updateCheck();
    setUpdateStatus(result);
    // The check itself is synchronous on the server, so `result` is already
    // the answer; the poke is for what follows it — an "installing" that wants
    // the busy cadence, and the disk re-read status() does on every poll.
    pokeUpdateStatus();
  } catch {
    // 404 (no updater), offline, server down — all of it is the poll's story.
  }
}

function ensureStarted(): void {
  if (started) return;
  started = true;
  // Registered once for the life of the page, alongside the one shared poll:
  // three surfaces subscribe to this store and none of them should own a
  // listener. `focus` catches the app being brought forward, and
  // `visibilitychange` catches a tab/window that was hidden becoming visible
  // without a focus event of its own.
  lastCheckTriggerAt = Date.now();
  window.addEventListener("focus", () => void onReturn());
  document.addEventListener("visibilitychange", () => void onReturn());
  poll();
}

/** Tests only: put the module back to its never-started state. The store is
 *  module-global by design (one poll for three surfaces), which is exactly what
 *  lets one test's "available" leak into the next test's "nothing here". Clears
 *  the timer too, so a finished test file leaves no poll behind to keep the
 *  runner's event loop alive. */
export function resetUpdateStatusForTests(): void {
  clearTimeout(timer);
  timer = undefined;
  generation += 1;
  current = null;
  started = false;
  listeners.clear();
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
