// THE CHAT'S PREFS, and the ONE `/api/prefs` GET every chat embed on the page
// shares. Today that is a single switch — `prefs.chat.recap`, the "While you
// were away" fold (shell/prefs.py `chat_recap_enabled`) — but the shared read
// is the reason this module exists at all: a tasks wall mounts six chats, and a
// reader per switch would be six round trips for one boolean.
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
 * DEFAULTS ON, which is what makes this a plain boolean rather than a
 * tri-state: nothing MOUNTS on this answer. It gates a fetch that cannot happen
 * until the reader has been away a minute, by which time the read has long
 * landed — so "not asked yet" and "on" are the same answer, and a server we
 * cannot reach is not a reason to drop the feature.
 */
let recap = true;
const recapListeners = new Set<(v: boolean) => void>();

function setRecap(next: boolean) {
  if (recap === next) return;
  recap = next;
  for (const listener of recapListeners) listener(next);
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
 * this answer (see `recap`), so a hung read shows the reader a recap fold that
 * is on, which is the default and is right. What it costs is the RE-ASK, and a
 * prefs PUT still publishes over it (`publishChatRecapEnabled`). Bounded all
 * the same, because "the page never asks again" should not be a state a
 * hung socket can put this module in.
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
      // `!== false`, never `=== true`: the recap is ON by default, so a server
      // that predates the field (or one whose prefs.json has never been
      // written) must read as on rather than silently losing the feature.
      setRecap(p.chat?.recap !== false);
    })
    .catch(() => {
      // STILL NO ANSWER, so the default stands and the fold is still offered.
      // `reading` is cleared so a later mount (or a publish) can ask again.
      reading = null;
    })
    .then(() => {});
  return reading;
}

/** Hand over a known-fresh answer for the recap switch (the prefs payload a PUT
 *  returned), rather than waiting for another GET. */
export function publishChatRecapEnabled(next: boolean) {
  setRecap(next);
}

/** Whether the session-recap fold is offered. Defaults ON — see `recap`. */
export function chatRecapEnabledNow(): boolean {
  return recap;
}

/** Subscribe to the recap switch. Triggers the one shared prefs read, so a
 *  second chat on the same page pays nothing for asking. */
export function useChatRecapEnabled(): boolean {
  const [current, setCurrent] = useState<boolean>(chatRecapEnabledNow);
  useEffect(() => {
    recapListeners.add(setCurrent);
    setCurrent(chatRecapEnabledNow());
    void read();
    return () => {
      recapListeners.delete(setCurrent);
    };
  }, []);
  return current;
}

/** Test-only: forget the cached answer so a suite starts from "not asked".
 *  NOTIFIES, like every other write: a component already mounted would
 *  otherwise keep the answer the suite just took away, and the `generation`
 *  bump is what lands a read still in flight from the previous test on the
 *  floor instead of on the next one's state. */
export function resetChatPrefsForTests() {
  reading = null;
  generation += 1;
  prefsDeadlineMs = GATE_FALLBACK_MS;
  setRecap(true);
}
