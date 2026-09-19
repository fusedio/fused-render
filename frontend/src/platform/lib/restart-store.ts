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
//   2. RECORD it in localStorage — the durable half, see below;
//   3. broadcast it, for the windows that are awake to hear it;
//   4. navigate the deep link.
//
// TWO CHANNELS, AND THE DURABLE ONE IS NOT THE BROADCAST. The first cut treated
// `BroadcastChannel` as the mechanism and localStorage as a fallback for
// contexts without one. That is backwards, and the real-app test of PR #1214
// showed why: the window that is NOT being clicked in is by definition in the
// background, and a background window is throttled and eventually frozen. A
// `postMessage` is a ONE-SHOT EVENT — a window that is not listening at that
// instant never learns of the press at all, so when its own probes start failing
// it tells the user the app "isn't running" while another window is mid-restart.
//
// So the localStorage key is the RECORD OF RECORD — a fact that says "a restart
// was asked for at this instant" — and it is re-read on start and again on every
// visibility/focus change, which is exactly when a throttled window wakes up.
// `BroadcastChannel` stays as the FAST path for the windows that are awake.
// Both carry the same payload and latching is idempotent on `at`, so a window
// that hears both is unaffected.
//
// The record is FRESHNESS-BOUNDED (`RESTART_GIVE_UP_MS`) and cleared the moment
// the flow ends, so it can never resurrect a finished restart: without that, the
// page that reloads onto the new version would adopt its own press back off
// disk and sit congratulating itself on a server that has been fine for a
// minute.
//
// The state machine itself is `restart-flow.ts` and is pure; nothing here
// decides what a probe means.
import { useSyncExternalStore } from "react";

import {
  initialRestart,
  reduceRestart,
  restartInFlight,
  RESTART_GIVE_UP_MS,
  type RestartEvent,
  type RestartState,
  type RestartStage,
} from "@platform/lib/restart-flow";

/** The deep link. `fused_render/deeplink.py` accepts it payload-free by
 *  definition; the OS hands it to the running app, which quits through the
 *  normal teardown (`app.begin_relaunch`) and respawns from the bundle now on
 *  disk. NOT `fda.ts`'s `RELAUNCH_HREF` — that one carries `?reason=fda` and
 *  respawns the SAME version to pick up a Full Disk Access grant, which is a
 *  different action with a different story on screen. */
export const RELAUNCH_HREF = "fused-render://relaunch";

export const RESTART_CHANNEL_NAME = "fused-render:restart";
export const RESTART_STORAGE_KEY = "fused_restart_requested_at";

/** How often the cap is re-checked with no probe to carry it. Probes land every
 *  5 s and would eventually do it, but the give-up moment is a promise about a
 *  clock and should not be rounded up to whenever the next fetch times out. */
const TICK_MS = 1_000;

/** How long `wake()` waits for the server before it gives up on an answer — the
 *  same budget `ServerStatusBanner`'s own probe uses (PROBE_TIMEOUT_MS). A hung
 *  request must not hold the dialog's button in limbo indefinitely, and a server
 *  that cannot answer inside four seconds is, for this decision, a server that is
 *  not answering: the record is adopted, exactly as for a refused connection. */
export const RESTART_ASK_TIMEOUT_MS = 4_000;

/** What a press looks like on the wire AND on disk — one shape for both
 *  channels, so a window cannot learn two different things about one press. */
export interface RestartRecord {
  at: number;
  served: string | null;
}

interface ChannelLike {
  postMessage(data: unknown): void;
  onmessage: ((ev: { data: unknown }) => void) | null;
  close(): void;
}

interface StorageLike {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

/** The pieces of the window a store talks to. Injected rather than reached for,
 *  so a test can run TWO stores against ONE fake channel and one fake storage —
 *  which is the only way to test the thing that actually broke: what the window
 *  that did NOT press ends up showing. */
export interface RestartStoreHost {
  channel?: () => ChannelLike | null;
  storage?: () => StorageLike | null;
  listen?: (type: string, fn: () => void) => void;
  navigate?: (href: string) => void;
  now?: () => number;
  /** What the server is serving RIGHT NOW. Only the record path uses it — see
   *  `wake`. `null` version means "could not tell". */
  ask?: () => Promise<{ ok: boolean; version: string | null }>;
  askTimeoutMs?: number;
}

/** What a surface reads: the stage machine's state, plus whether this window is
 *  still ASKING the server about a record it found. `verifying` exists so the
 *  dialog does not offer a button during that gap — a press in it would start a
 *  SECOND restart on top of the one being verified. */
export type RestartView = RestartState & { verifying: boolean };

export interface RestartStore {
  requestRestart(): void;
  noteRestartProbe(probe: { ok: boolean; version?: string | null }): void;
  subscribe(fn: () => void): () => void;
  snapshot(): RestartView;
  /** Deliver a message as the channel would — the seam a second window's test
   *  double posts into. */
  receive(data: unknown): void;
  /** Re-read the durable record, as a wake-up does. Async because adopting one
   *  means asking the server whether the restart already finished. */
  wake(): Promise<void>;
  /** Drop the durable record — the reload path calls this on its way out, so a
   *  restart that has already handed the page to a new server leaves nothing
   *  behind for the fresh document to adopt. */
  forget(): void;
  dispose(): void;
}

function defaultHost(): Required<RestartStoreHost> {
  return {
    channel: () => {
      try {
        return new BroadcastChannel(RESTART_CHANNEL_NAME) as unknown as ChannelLike;
      } catch {
        // No BroadcastChannel (an old WebView, a locked-down context) — the
        // durable record below is the whole cross-window story there, and it is
        // the half that matters anyway.
        return null;
      }
    },
    storage: () => {
      try {
        return window.localStorage;
      } catch {
        // Private window, blocked site data. The broadcast is then the only
        // path, and a background window will miss the press — nothing else can
        // be done from here.
        return null;
      }
    },
    listen: (type, fn) => {
      try {
        if (type === "visibilitychange") document.addEventListener(type, fn);
        else window.addEventListener(type, fn);
      } catch {
        // No window/document to listen on (the test renderer's bare shim).
      }
    },
    navigate: (href) => {
      try {
        window.location.assign(href);
      } catch {
        // No `location` (the test renderer). The latch and the record are the
        // parts under test; a navigation that cannot happen is not an error
        // worth throwing out of a click handler.
      }
    },
    now: () => Date.now(),
    askTimeoutMs: RESTART_ASK_TIMEOUT_MS,
    ask: async () => {
      // Aborted on the same budget the store races it against, so a hung request
      // is not merely ignored — it is cancelled, and does not sit on a
      // connection for the life of the page.
      const ctrl = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), RESTART_ASK_TIMEOUT_MS);
      try {
        const res = await fetch("/api/config", { cache: "no-store", signal: ctrl.signal });
        if (!res.ok) return { ok: false, version: null };
        const body = await res.json();
        return { ok: true, version: typeof body.version === "string" ? body.version : null };
      } catch {
        // The server is not answering — which is what a restart in flight looks
        // like, and the caller reads it that way.
        return { ok: false, version: null };
      } finally {
        clearTimeout(timer);
      }
    },
  };
}

export function createRestartStore(host: RestartStoreHost = {}): RestartStore {
  const h = { ...defaultHost(), ...host };
  let state: RestartState = initialRestart();
  /** The last version a healthy probe reported — what a press hands the reducer
   *  as "the version that was running", so no call site has to know one. */
  let lastServed: string | null = null;
  const listeners = new Set<() => void>();
  let channel: ChannelLike | null = null;
  let ticker: ReturnType<typeof setInterval> | undefined;
  let started = false;
  /** The `at` of the record currently being verified, or null. Doubles as the
   *  "a wake is in flight" flag and as the guard against two overlapping wakes
   *  verifying the same record twice. */
  let verifyingAt: number | null = null;
  /** Cached so `snapshot` returns a stable reference between notifications —
   *  `useSyncExternalStore` re-renders every subscriber otherwise. */
  let view: RestartView = { ...state, verifying: false };

  function publishView(): void {
    view = { ...state, verifying: verifyingAt !== null };
    listeners.forEach((fn) => fn());
  }

  function read(): RestartRecord | null {
    const store = h.storage();
    if (!store) return null;
    try {
      const raw = store.getItem(RESTART_STORAGE_KEY);
      if (!raw) return null;
      const body = JSON.parse(raw) as { at?: unknown; served?: unknown };
      if (typeof body.at !== "number" || !Number.isFinite(body.at)) return null;
      return { at: body.at, served: typeof body.served === "string" ? body.served : null };
    } catch {
      // A key written by something else, or half-written.
      return null;
    }
  }

  function write(record: RestartRecord | null): void {
    const store = h.storage();
    if (!store) return;
    try {
      if (record === null) store.removeItem(RESTART_STORAGE_KEY);
      else store.setItem(RESTART_STORAGE_KEY, JSON.stringify(record));
    } catch {
      // Quota, private window — the broadcast still carries this press to the
      // windows that are awake.
    }
  }

  function dispatch(event: RestartEvent): void {
    const next = reduceRestart(state, event, h.now());
    // BY VALUE, not identity: the reducer hands back a fresh object for a probe
    // that only reset the failure count, and `snapshot` must return a stable
    // reference between notifications or `useSyncExternalStore` re-renders every
    // subscriber on every poll tick (the trap `update-status.ts`'s `set`
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
    // THE RECORD DIES WITH THE FLOW. `back` and `gave-up` are both ENDINGS —
    // there is no longer a restart for another window to join — and a record
    // that outlived them would be adopted by the very page that reloads onto
    // the new version, putting a blocking dialog up to congratulate the user on
    // a server that has been fine for a minute. Note this is NOT
    // `restartInFlight`: `back` is still on screen (the reload is a beat away)
    // and is nonetheless the end of the story.
    const over = (s: RestartState) => s.stage === "back" || s.stage === "gave-up";
    const ending = !over(state) && over(next);
    state = next;
    if (ending) write(null);
    arm();
    if (changed) publishView();
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

  /** Adopt a press — this window's own, another window's over the channel, or
   *  one read back off the durable record. Idempotent on the instant: the same
   *  `at` arriving twice must not restart the cap. */
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

  /** `h.ask()`, bounded. An injected host may hang forever and a real fetch can
   *  too; a hung request must not hold the dialog's button in limbo. A timeout
   *  is treated as "not answering", which is the same answer a refused
   *  connection gives and means the same thing here: the app is between
   *  processes, so the record is adopted. */
  async function askBounded(): Promise<{ ok: boolean; version: string | null }> {
    const budget = h.askTimeoutMs ?? RESTART_ASK_TIMEOUT_MS;
    let timer: ReturnType<typeof setTimeout> | undefined;
    try {
      return await Promise.race([
        h.ask(),
        new Promise<{ ok: boolean; version: string | null }>((resolve) => {
          timer = setTimeout(() => resolve({ ok: false, version: null }), budget);
        }),
      ]);
    } finally {
      clearTimeout(timer);
    }
  }

  /**
   * Re-read the durable record. Called on start and on every wake-up
   * (visibilitychange / focus), which is the case a broadcast cannot cover: a
   * background window is throttled and eventually frozen, so a `postMessage`
   * sent while it was asleep is simply never heard.
   *
   * IT ASKS THE SERVER BEFORE IT BELIEVES THE RECORD, because a record is
   * evidence that a restart was ASKED FOR, not that one is still running — and
   * what it would raise is a dialog with no ✕, no Esc and no backdrop. The
   * clear-on-ending path cannot be the only guard: a document that reloads
   * without ever passing through `back` (a press made before any healthy probe,
   * a discarded tab restoring onto the new server) starts from nothing and would
   * adopt a finished press straight back off disk (bugbot, PR #1214).
   *
   * The three answers:
   *   • the server is serving a DIFFERENT version than the record's — the
   *     restart already completed, so forget it and latch nothing;
   *   • the server is serving the SAME version — the old process is still up,
   *     which is exactly a restart in flight: latch;
   *   • the server does not answer, or does not answer in time — also a restart
   *     in flight (the app is between processes): latch.
   * A record with no `served` on it cannot be compared at all, so it is treated
   * as finished: never raise an undismissable dialog you cannot justify.
   *
   * EVERYTHING IT DECIDES IS STALE BY THE TIME IT DECIDES IT (bugbot, PR #1214).
   * The request takes a moment, and in that moment a press can land here or a
   * broadcast can arrive from another window — both NEWER than the record this
   * call read. Writing `null` then would destroy the new press's record, and
   * latching the old `at` would expire the cap early and drop other windows off
   * the story. So the state and the record are BOTH re-read afterwards and the
   * whole result is discarded unless nothing moved.
   */
  async function wake(): Promise<void> {
    const record = read();
    if (record === null) return;
    if (h.now() - record.at > RESTART_GIVE_UP_MS) {
      write(null);
      return;
    }
    // Already living this one — nothing to verify and nothing to change.
    if (state.requestedAt === record.at && state.stage !== "ready") return;
    if (!record.served) {
      write(null);
      return;
    }
    // One outcome per record: a second wake landing on the same one (focus and
    // visibilitychange both fire on the same gesture) joins rather than races.
    if (verifyingAt === record.at) return;

    const wasAt = state.requestedAt;
    verifyingAt = record.at;
    publishView();
    let seen: { ok: boolean; version: string | null };
    try {
      seen = await askBounded();
    } finally {
      verifyingAt = null;
    }

    // Anything newer than what this call read wins outright.
    const nowRecord = read();
    const moved =
      state.requestedAt !== wasAt || nowRecord === null || nowRecord.at !== record.at;
    if (moved) {
      publishView();
      return;
    }
    if (seen.ok && seen.version && seen.version !== record.served) {
      write(null);
      publishView();
      return;
    }
    latch(record.at, record.served);
    publishView();
  }

  function start(): void {
    if (started) return;
    started = true;
    channel = h.channel();
    if (channel) channel.onmessage = (ev) => receive(ev.data);
    // Both events, because neither covers the other: `focus` catches a window
    // brought forward, `visibilitychange` catches one that was occluded becoming
    // visible without ever taking focus.
    h.listen("focus", () => void wake());
    h.listen("visibilitychange", () => void wake());
    void wake();
  }

  return {
    requestRestart() {
      start();
      const at = h.now();
      const record: RestartRecord = { at, served: lastServed };
      // Local, then durable, then fast — the navigation hands the main thread to
      // the OS, so anything left after it is a race.
      latch(at, lastServed);
      write(record);
      try {
        channel?.postMessage(record);
      } catch {
        // A channel closed under us — the record above still carries it.
      }
      h.navigate(RELAUNCH_HREF);
    },
    noteRestartProbe(probe) {
      start();
      if (probe.ok && typeof probe.version === "string" && probe.version) {
        lastServed = probe.version;
      }
      dispatch({ type: "probe", ok: probe.ok, version: probe.version ?? null });
    },
    subscribe(fn) {
      start();
      listeners.add(fn);
      return () => {
        listeners.delete(fn);
      };
    },
    snapshot: () => view,
    receive,
    wake,
    forget: () => write(null),
    dispose() {
      clearInterval(ticker);
      ticker = undefined;
      try {
        channel?.close();
      } catch {
        // Already closed.
      }
      channel = null;
      state = initialRestart();
      view = { ...state, verifying: false };
      verifyingAt = null;
      lastServed = null;
      listeners.clear();
      started = false;
    },
  };
}

// ---- the window's own store ------------------------------------------------
// One per document, which is what "every open window agrees" means. The factory
// above exists so a TEST can hold two at once, not so the app can.

let store = createRestartStore();

export function requestRestart(): void {
  store.requestRestart();
}

/** One probe result from `ServerStatusBanner`'s poll — the only clock this store
 *  reads apart from its own tick. Called for every probe, in flight or not, so
 *  `lastServed` is current the moment someone presses the button. */
export function noteRestartProbe(probe: { ok: boolean; version?: string | null }): void {
  store.noteRestartProbe(probe);
}

/** The stage right now, for code that is not a component — the banner's poll
 *  timer asks this to decide whether a hidden tab still probes. */
export function restartStageNow(): RestartStage {
  return store.snapshot().stage;
}

export function useRestartFlow(): RestartView {
  return useSyncExternalStore(
    (fn) => store.subscribe(fn),
    () => store.snapshot(),
    () => store.snapshot(),
  );
}

/** Tests only — the store is module-global by design (one press, every surface),
 *  which is exactly what lets one test's press leak into the next test's
 *  "nothing has happened". */
export function resetRestartForTests(): void {
  store.dispose();
  store = createRestartStore();
}

/** Tests only — drive the cross-window path without a second real window. */
export function receiveRestartBroadcastForTests(data: unknown): void {
  store.receive(data);
}

/** The page is about to reload onto whatever the server is serving now, so the
 *  restart this record describes is over by definition. Called on
 *  `ServerStatusBanner`'s reload path: the fresh document that comes up a moment
 *  later must not read the press back off disk and put the dialog up over a
 *  server that is already fine. */
export function forgetRestartRecord(): void {
  store.forget();
}
