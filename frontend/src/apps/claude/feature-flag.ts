// The project queue switch — one task in progress per folder
// (`prefs.queue.enabled`, shell/prefs.py `project_queue_enabled`). Clone of
// apps/canvases/feature-flag.ts: one shared GET, a generation guard so a
// publish or a re-read beats a slower in-flight read.
//
// Here rather than in a module of its own: every chat embed already causes
// exactly one `/api/prefs` GET through this read, and the composer asks this on
// the keystroke that sends — a second reader would be a second round trip per
// mount for one boolean.
//
// A SEPARATE MODULE FROM index.ts on purpose: the barrel re-exports ChatMount,
// and a host reading the flag through it would pull the chat's mount into its
// bundle for one boolean.
import { useEffect, useState } from "react";
import { getPrefs, getPrefsShared } from "@platform/lib/api";
import { GATE_FALLBACK_MS } from "@platform/lib/clock";

let reading: Promise<void> | null = null;
let generation = 0;

/**
 * NOT a tri-state. It gates ONE extra call in front of a send, it DEFAULTS
 * OFF, and off is exactly what every send did before the feature existed — so
 * "not asked yet" and "off" are the same answer, and the worst a send inside
 * the first read's window can do is behave like today (and `queueFlagReady`
 * closes even that window for a send).
 */
let queue = false;
const queueListeners = new Set<(v: boolean) => void>();
/** A read has SETTLED at least once this page — landed or failed. After that,
 *  `queueEnabled()` is an answer and not a guess, and a send does not wait on
 *  another round trip (Bugbot: a failed GET used to null `reading`, so every
 *  send and decide waited up to 8 s until one succeeded). */
let settledOnce = false;

function setQueue(next: boolean) {
  if (queue === next) return;
  queue = next;
  for (const listener of queueListeners) listener(next);
}

/**
 * A PREFS READ THAT NEVER ANSWERS IS NOT A FAILURE — it is worse, because
 * nothing catches it (2026-09-15).
 *
 * `getPrefs` rejects on a refused connection, and the retry and the `catch`
 * below both handle that. What neither handles is a request the server ACCEPTS
 * and never answers — a wedged worker, a machine that went to sleep mid-flight,
 * a paused process — where the promise simply never settles, and every send
 * awaiting `queueFlagReady` would wait with it.
 *
 * So the read is raced with the same 8 s backstop every other gate in this app
 * has (`platform/lib/clock.GATE_FALLBACK_MS`). Losing the race is treated
 * exactly as a failed read is — the default the pref itself has — and
 * `reading` is cleared either way, so the next ask goes again rather than
 * inheriting a verdict taken while the server was away.
 */
/** The budget itself, as a variable only so a test can make it small: eight
 *  seconds of real time per case is not a test anyone runs. Production never
 *  moves it — `setPrefsDeadlineForTests` is the only writer, and
 *  `resetFlagsForTests` puts it back. */
let prefsDeadlineMs: number = GATE_FALLBACK_MS;

/** Test seam — see `prefsDeadlineMs`. Pass nothing to restore the real budget. */
export function setPrefsDeadlineForTests(ms: number = GATE_FALLBACK_MS) {
  prefsDeadlineMs = ms;
}

function withDeadline<T>(work: Promise<T>, ms: number): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    let settled = false;
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      reject(new Error("prefs read timed out"));
    }, ms);
    work.then(
      (value) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        resolve(value);
      },
      (e) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        reject(e instanceof Error ? e : new Error(String(e)));
      },
    );
  });
}

function read(): Promise<void> {
  if (reading) return reading;
  const departed = generation;
  // ONE BUDGET FOR THE WHOLE READ, retry included (bugbot, 2026-09-15).
  //
  // The deadline used to wrap each ATTEMPT, so a wedged server spent 8 s, was
  // told it had failed, and was asked a second time for another 8 s: sixteen
  // seconds, twice the number this constant names. Worse, the first attempt was
  // ABANDONED at 8 s, so a GET that landed at 8.1 s — which is exactly what a
  // slow cold start looks like — was thrown away in favour of a fresh request.
  //
  // So the budget goes around BOTH attempts. The retry is still there and still
  // worth one round trip — a prefs GET that REJECTS is usually a single dropped
  // request racing the server's start, and that rejection is fast — but it now
  // spends what is left of the eight seconds rather than opening a second
  // eight-second window, and a read that is merely slow is never given up on in
  // favour of asking again.
  reading = withDeadline(
    getPrefsShared().catch(() => getPrefs()),
    prefsDeadlineMs,
  )
    .then((p) => {
      if (generation !== departed) return;
      // `=== true`: the queue is OPT-IN, so a server with no such field is a
      // server whose sends are not admitted through anything.
      setQueue(p.queue?.enabled === true);
    })
    .catch(() => {
      // A failed read keeps whatever answer the page already has (the default,
      // off, on a first read): a blind write here on one refused GET after a
      // laptop wake would flip a live switch. And `reading` is cleared only by
      // the read that owns it, never by a superseded one.
      if (generation !== departed) return;
      reading = null;
    })
    .then(() => {
      if (generation === departed) settledOnce = true;
    });
  return reading;
}

/**
 * THE FLAG IS READ, NOT GUESSED, BEFORE A SEND ASKS IT (Akshil's QA, 2026-09-16).
 *
 * `queueEnabled()` answers `false` until the one prefs read lands, and "off"
 * was argued to be the safe default — every send behaved like today. It is
 * not safe with the queue ON: a send made inside that window skipped the
 * queue's door and started a second run in a busy folder. So the send path
 * awaits this first: the in-flight read, or a fresh one when nothing has
 * asked yet. Bounded by the same 8 s backstop every read here has; a read that
 * failed leaves the flag at its default, which is what it would have been.
 */
export function queueFlagReady(): Promise<void> {
  if (reading) return reading;
  if (settledOnce) return Promise.resolve();
  return read();
}

/**
 * Ask the server again. The one read per page load was the right economy for
 * a flag that never moved under a page; this one is flipped in Settings, and
 * a TAB THAT WAS ALREADY OPEN went on sending by the old answer (Akshil's QA,
 * 2026-09-16: the beta tab never queued). Called when the window comes back
 * into view — the moment a reader who toggled the pref elsewhere returns.
 */
export function rereadFlags(): Promise<void> {
  // A NEW GENERATION, so the read this replaces cannot speak after it (Bugbot).
  generation += 1;
  reading = null;
  return read();
}

/** The `localStorage` key a Settings toggle announces itself on, so every
 *  OTHER tab of this app hears the flip through the `storage` event instead of
 *  keeping the answer it read at load. */
export const QUEUE_FLAG_BROADCAST_KEY = "fused-render:project-queue";

/** Hand over a known-fresh answer (the prefs payload a PUT returned). */
export function publishProjectQueueEnabled(next: boolean) {
  setQueue(next);
  try {
    localStorage.setItem(QUEUE_FLAG_BROADCAST_KEY, JSON.stringify({ on: next, at: Date.now() }));
  } catch {
    // Storage may be unavailable; the other tabs still re-read on focus.
  }
}

/** One `storage` event, as the listener below sees it. Exported so the rule
 *  can be exercised where the test DOM has no `StorageEvent`. */
export function applyQueueFlagBroadcast(key: string | null, newValue: string | null): void {
  if (key !== QUEUE_FLAG_BROADCAST_KEY || !newValue) return;
  try {
    setQueue((JSON.parse(newValue) as { on?: unknown }).on === true);
  } catch {
    // A malformed broadcast is ignored; the next focus re-reads.
  }
}

if (typeof window !== "undefined") {
  window.addEventListener("storage", (ev) => applyQueueFlagBroadcast(ev.key, ev.newValue));
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") void rereadFlags();
  });
}

/** Is the project queue on RIGHT NOW — the question a SEND asks, in the same
 *  tick it is dispatched in, which is why this is a plain read and not a hook.
 *  Defaults false; see `queue` for why that is an answer and not an absence. */
export function queueEnabled(): boolean {
  return queue;
}

/** Subscribe to the project queue switch. Triggers the one prefs read, so a
 *  chat that is already mounted pays nothing for asking. */
export function useProjectQueueEnabled(): boolean {
  const [current, setCurrent] = useState<boolean>(queueEnabled);
  useEffect(() => {
    queueListeners.add(setCurrent);
    setCurrent(queueEnabled());
    void read();
    return () => {
      queueListeners.delete(setCurrent);
    };
  }, []);
  return current;
}

/** Test-only: how many components are currently subscribed. `bun test` runs
 *  every suite in ONE process, so this Set is shared globally for the run —
 *  a tree a test forgets to unmount leaves its subscription here forever,
 *  which is exactly the bug this exists to make loud (see sched-block.test.tsx's
 *  `afterEach`, and DECISIONS.md's "bun test heap leak" entry). */
export function listenerCountsForTests() {
  return { queueListeners: queueListeners.size };
}

/** Test-only: forget the cached answer so a suite starts from "not asked".
 *  NOTIFIES, like every other write: a component already mounted would
 *  otherwise keep the answer the suite just took away. */
export function resetFlagsForTests() {
  reading = null;
  settledOnce = false;
  generation += 1;
  prefsDeadlineMs = GATE_FALLBACK_MS;
  setQueue(false);
}
