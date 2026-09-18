// THE ONE WAY TO RESTART THE APP. Every surface that used to render its own
// `fused-render://relaunch` link — the sidebar badge's "Restart fused-render",
// the Settings popover's "Ready to restart" row, and the banner card that is
// now gone — calls `requestRestart()` here instead. Not for tidiness: a press
// has to be REMEMBERED (the deep link answers nothing, so the outage that
// follows is the only evidence the app is coming back) and it has to be
// remembered in EVERY OPEN WINDOW (D3), and a link in the markup can do
// neither.
//
// What one press does, in this order:
//   1. latch `requestedAt` locally, so this window's own stage machine starts
//      before the navigation takes the main thread away;
//   2. broadcast it, so every other window latches the SAME instant and runs
//      the same cap off its own probes;
//   3. navigate the deep link.
//
// CROSS-WINDOW BY BroadcastChannel, WITH A localStorage FALLBACK. The app has
// no channel of its own to borrow — `presence.ts` says in its own header that
// it is deliberately server-backed rather than a BroadcastChannel, and the
// half-dozen `storage`-event listeners in the shell each carry their own key —
// so this ships both halves and prefers the channel: `storage` does not fire in
// the window that wrote the key (which is why the local latch is step 1, not a
// consequence of the write), and a private window can throw on the accessor.
// Both paths carry the same payload, and latching is idempotent on
// `requestedAt`, so a window that somehow hears both is unaffected.
//
// The state machine itself is `restart-flow.ts` and is pure; nothing here
// decides what a probe means.
import { useSyncExternalStore } from "react";

import {
  initialRestart,
  reduceRestart,
  restartInFlight,
  type RestartEvent,
  type RestartState,
} from "@platform/lib/restart-flow";

/** The deep link. `fused_render/deeplink.py` accepts it payload-free by
 *  definition; the OS hands it to the running app, which quits through the
 *  normal teardown (`app.begin_relaunch`) and respawns from the bundle now on
 *  disk. NOT `fda.ts`'s `RELAUNCH_HREF` — that one carries `?reason=fda` and
 *  respawns the SAME version to pick up a Full Disk Access grant, which is a
 *  different action with a different story on screen. */
export const RELAUNCH_HREF = "fused-render://relaunch";

const CHANNEL_NAME = "fused-render:restart";
const STORAGE_KEY = "fused_restart_requested_at";

/** How often the cap is re-checked with no probe to carry it. Probes land every
 *  5 s and would eventually do it, but the give-up moment is a promise about a
 *  clock and should not be rounded up to whenever the next fetch times out. */
const TICK_MS = 1_000;

let state: RestartState = initialRestart();
/** The last version a healthy probe reported — what `requestRestart` hands the
 *  reducer as "the version that was running when the button was pressed", so no
 *  caller has to know one. */
let lastServed: string | null = null;
const listeners = new Set<() => void>();
let started = false;
let channel: BroadcastChannel | null = null;
let ticker: ReturnType<typeof setInterval> | undefined;

function emit(): void {
  listeners.forEach((fn) => fn());
}

function dispatch(event: RestartEvent): void {
  const next = reduceRestart(state, event, Date.now());
  // BY VALUE, not identity: the reducer hands back a fresh object for a probe
  // that only reset the failure count, and `getSnapshot` must return a stable
  // reference between notifications or `useSyncExternalStore` re-renders every
  // subscriber on every poll tick (the same trap `update-status.ts`'s `set`
  // documents).
  if (
    next.stage === state.stage &&
    next.requestedAt === state.requestedAt &&
    next.fails === state.fails &&
    next.before === state.before
  ) {
    return;
  }
  const changed = next.stage !== state.stage || next.requestedAt !== state.requestedAt;
  state = next;
  arm();
  if (changed) emit();
}

/** Keep the clock running exactly while there is a wait to time out. */
function arm(): void {
  const wanted = restartInFlight(state.stage);
  if (wanted && ticker === undefined) {
    ticker = setInterval(() => dispatch({ type: "tick" }), TICK_MS);
  } else if (!wanted && ticker !== undefined) {
    clearInterval(ticker);
    ticker = undefined;
  }
}

/** Adopt a press — this window's own or another window's. Idempotent on the
 *  instant: the same `requestedAt` arriving twice (channel and storage both
 *  heard, say) must not restart the cap. */
function latch(at: number, served: string | null): void {
  if (state.requestedAt === at && state.stage !== "ready") return;
  dispatch({ type: "request", at, served });
}

function receive(data: unknown): void {
  if (!data || typeof data !== "object") return;
  const body = data as { at?: unknown; served?: unknown };
  if (typeof body.at !== "number" || !Number.isFinite(body.at)) return;
  latch(body.at, typeof body.served === "string" ? body.served : null);
}

function ensureStarted(): void {
  if (started) return;
  started = true;
  try {
    channel = new BroadcastChannel(CHANNEL_NAME);
    channel.onmessage = (ev: MessageEvent) => receive(ev.data);
  } catch {
    // No BroadcastChannel (an old WebView, a locked-down context) — the
    // storage half below is the whole cross-window story there.
    channel = null;
  }
  try {
    window.addEventListener("storage", (ev: StorageEvent) => {
      if (ev.key !== STORAGE_KEY || !ev.newValue) return;
      try {
        receive(JSON.parse(ev.newValue));
      } catch {
        // A key written by something else, or half-written — nothing to adopt.
      }
    });
  } catch {
    // No window to listen on (the test renderer's bare shim).
  }
}

function publish(at: number, served: string | null): void {
  const body = { at, served };
  try {
    channel?.postMessage(body);
  } catch {
    // A channel closed under us — the storage write below still carries it.
  }
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(body));
  } catch {
    // Private window, blocked site data: the channel is the fallback's
    // fallback. Losing the cross-window echo costs the OTHER windows their
    // stages, never this one its restart.
  }
}

/**
 * THE PRESS. Latch, tell the other windows, then navigate — in that order,
 * because `location.assign` hands the main thread to the OS and anything left
 * after it is a race.
 *
 * Takes nothing: the served version comes from the probes this store is already
 * being fed (`noteRestartProbe`), so every call site is identical and no
 * surface can pass a different idea of what is running.
 */
export function requestRestart(): void {
  ensureStarted();
  const at = Date.now();
  latch(at, lastServed);
  publish(at, lastServed);
  try {
    window.location.assign(RELAUNCH_HREF);
  } catch {
    // No `location` (the test renderer). The latch above is the part under
    // test; a navigation that cannot happen is not an error worth throwing
    // out of a click handler.
  }
}

/** One probe result from `ServerStatusBanner`'s poll — the only clock this
 *  store reads apart from its own tick. Called for every probe, in flight or
 *  not, so `lastServed` is current the moment someone presses the button. */
export function noteRestartProbe(probe: { ok: boolean; version?: string | null }): void {
  ensureStarted();
  if (probe.ok && typeof probe.version === "string" && probe.version) lastServed = probe.version;
  dispatch({ type: "probe", ok: probe.ok, version: probe.version ?? null });
}

function subscribe(fn: () => void): () => void {
  ensureStarted();
  listeners.add(fn);
  return () => {
    listeners.delete(fn);
  };
}

function getSnapshot(): RestartState {
  return state;
}

export function useRestartFlow(): RestartState {
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
}

/** Tests only — the store is module-global by design (one press, every
 *  surface), which is exactly what lets one test's press leak into the next
 *  test's "nothing has happened". */
export function resetRestartForTests(): void {
  clearInterval(ticker);
  ticker = undefined;
  state = initialRestart();
  lastServed = null;
  listeners.clear();
  try {
    channel?.close();
  } catch {
    // Already closed.
  }
  channel = null;
  started = false;
}

/** Tests only — drive the cross-window path without a second real window. */
export function receiveRestartBroadcastForTests(data: unknown): void {
  receive(data);
}
