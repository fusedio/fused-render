// THE CHAT'S PREFS, and the ONE `/api/prefs` GET every chat embed on the page
// shares. One switch today — `prefs.queue.enabled`, the project queue
// (`project_queue_enabled`) — and the shared read is the reason this module
// exists at all: a tasks wall mounts six chats, and a reader per switch would
// be six round trips for one boolean.
//
// The "While you were away" session-recap fold used to be a second switch
// here (`prefs.chat.recap`, `useChatRecapEnabled`); the Preferences toggle
// that gated it left on 2026-09-21 — recap is simply on for every native
// chat now, so there is nothing left for this module to read for it.
//
// A SEPARATE MODULE FROM index.ts on purpose: the barrel re-exports ClaudeChat,
// and a host reading a pref through it would pull the whole chat (and the
// markdown chunk) into its bundle for one boolean.
import { useEffect, useState } from "react";
import { getPrefs } from "@platform/lib/api";
import { GATE_FALLBACK_MS } from "@platform/lib/clock";

let reading: Promise<void> | null = null;
let generation = 0;

/**
 * THE ONE SWITCH THIS PREFS READ ANSWERS: `prefs.queue.enabled`, the project
 * queue — one task in progress per folder (shell/prefs.py
 * `project_queue_enabled`).
 *
 * Its own module-level cache for the reason stated up top: every chat embed
 * already causes exactly one `/api/prefs` GET, and the composer asks this on
 * the keystroke that sends — a second reader would be a second round trip per
 * mount for one boolean.
 *
 * DEFAULTS OFF, because off is exactly what every send did before the feature
 * existed — so "not asked yet" and "off" are the same answer, and the worst a
 * send inside the first read's window can do is behave like today.
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
 * nothing catches it (ported from the `native_chat_enabled` gate's own fix,
 * #1162, when that gate was deleted).
 *
 * `getPrefs` rejects on a refused connection, and the retry and the `catch`
 * below both handle that. What neither handles is a request the server ACCEPTS
 * and never answers — a wedged worker, a machine that went to sleep mid-flight,
 * a paused process — where the promise simply never settles. `reading` then
 * stays pinned to a promise that never resolves for the life of the page, so
 * `read` short-circuits on it forever and NO LATER MOUNT CAN EVER ASK AGAIN:
 * the one thing the `catch` below exists to guarantee is exactly the thing a
 * hang takes away.
 *
 * Milder here than it was on the flag this was ported from — nothing MOUNTS on
 * this answer (see `queue`), so a hung read leaves the queue at its default
 * (off, which is what every send did before the feature existed), and
 * `queueFlagReady` simply resolves once the deadline fires. What it costs is
 * the RE-ASK, and a prefs PUT still publishes over it
 * (`publishProjectQueueEnabled`). Bounded all the same, because "the page
 * never asks again" should not be a state a hung socket can put this module
 * in.
 *
 * The budget goes around BOTH attempts, not each: that is the shape #1162
 * settled on, and it is the one that cannot turn one wedged server into two
 * full waits, or abandon a merely-slow read in favour of asking again.
 */
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

/** The budget itself, as a variable only so a test can make it small: eight
 *  seconds of real time per case is not a test anyone runs. Production never
 *  moves it — `setPrefsDeadlineForTests` is the only writer, and
 *  `resetChatPrefsForTests` puts it back. */
let prefsDeadlineMs: number = GATE_FALLBACK_MS;

/** Test seam — see `prefsDeadlineMs`. Pass nothing to restore the real budget. */
export function setPrefsDeadlineForTests(ms: number = GATE_FALLBACK_MS) {
  prefsDeadlineMs = ms;
}

function read(): Promise<void> {
  if (reading) return reading;
  const departed = generation;
  reading = withDeadline(
    getPrefs()
      // ONE BOUNDED RETRY, then whatever we have. A prefs GET that fails is
      // usually a single dropped request (a reload racing the server's start),
      // so asking twice is worth one round trip — and it spends what is left of
      // the one budget above rather than opening a second window.
      .catch(() => getPrefs()),
    prefsDeadlineMs,
  )
    .then((p) => {
      if (generation !== departed) return;
      // `=== true`: this is OPT-IN, so a server with no such field is a server
      // whose sends are not admitted through anything.
      setQueue(p.queue?.enabled === true);
    })
    .catch(() => {
      // STILL NO ANSWER, so the default stands. `reading` is cleared — by the
      // read that OWNS it and never by a superseded one — so a later mount (or
      // a publish) can ask again.
      if (generation !== departed) return;
      reading = null;
    })
    .then(() => {
      if (generation === departed) settledOnce = true;
    });
  return reading;
}

/** Hand over a known-fresh answer for the project queue (the prefs payload a
 *  PUT returned), rather than waiting for another GET. Also BROADCAST, so
 *  every other tab of this app hears the flip through the `storage` event
 *  instead of keeping the answer it read at load. */
export function publishProjectQueueEnabled(next: boolean) {
  setQueue(next);
  try {
    localStorage.setItem(QUEUE_FLAG_BROADCAST_KEY, JSON.stringify({ on: next, at: Date.now() }));
  } catch {
    // Storage may be unavailable; the other tabs still re-read on focus.
  }
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
 * Ask the server again. The one read per page load was the right economy for a
 * switch that never moved under a page; the queue is flipped in Settings, and a
 * TAB THAT WAS ALREADY OPEN went on sending by the old answer (Akshil's QA,
 * 2026-09-16: the beta tab never queued). Called when the window comes back
 * into view — the moment a reader who toggled the pref elsewhere returns.
 */
export function rereadFlags(): Promise<void> {
  // A NEW GENERATION, so the read this replaces cannot speak after it: without
  // the bump an older GET that later timed out still matched `generation` and
  // wrote over a newer read that had already succeeded.
  generation += 1;
  reading = null;
  return read();
}

/** The `localStorage` key a Settings toggle announces itself on. */
export const QUEUE_FLAG_BROADCAST_KEY = "fused-render:project-queue";

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

/** Subscribe to the project queue switch. Triggers the one shared prefs
 *  read, so a chat that is already mounted pays nothing for asking. */
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
 *  every suite in ONE process, so this Set is shared globally for the run — a
 *  tree a test forgets to unmount leaves its subscription here forever, which
 *  is exactly the bug this exists to make loud (see `sched-block.test.tsx`'s
 *  `afterEach`, and DECISIONS.md's "bun test heap leak" entry — ported from
 *  the `native_chat_enabled` gate's own `feature-flag.ts` accessor, D890). */
export function listenerCountsForTests() {
  return { queueListeners: queueListeners.size };
}

/** Test-only: forget the cached answer so a suite starts from "not asked".
 *  NOTIFIES, like every other write: a component already mounted would
 *  otherwise keep the answer the suite just took away, and the `generation`
 *  bump is what lands a read still in flight from the previous test on the
 *  floor instead of on the next one's state. */
export function resetChatPrefsForTests() {
  reading = null;
  settledOnce = false;
  generation += 1;
  prefsDeadlineMs = GATE_FALLBACK_MS;
  setQueue(false);
}
