// Whether chat embeds render the native React chat (`apps/claude`) instead of
// the legacy `templates/claude` iframe — the `native_chat_enabled` pref
// (shell/prefs.py), env override `FUSED_RENDER_NATIVE_CHAT` applied server-side.
// Clone of apps/canvases/feature-flag.ts: one shared GET, a generation guard so
// a publish beats a slower in-flight read, and `null` meaning "not asked yet".
//
// THE TRI-STATE IS THE POINT FOR A MOUNT (`useNativeChatFlag`). Canvases only
// hides a menu entry while the answer is in flight, so `null` there reads as OFF
// and costs nothing; here a premature `false` mounts a whole `/render` template
// DOCUMENT, which boots, restores a session, drains
// `window._fusedClaudeAskTake` and starts a poll — and is then thrown away the
// instant the GET lands, taking the pending "Fix with AI" ask with it. So a
// MOUNT waits for a real answer (ChatMount covers the box meanwhile) and only a
// host asking a side question — "is a legacy iframe the thing owning params
// here?" — takes the boolean, where "not asked yet" genuinely does read as off.
//
// WHICH MAKES A FAILED READ AN ANSWER, NOT AN ABSENCE: `null` is only ever
// "in flight", so `read()` retries once and then settles on `false` (see the
// `catch`). A tri-state that could stick at `null` would hold a skeleton over
// every embed on the page forever.
//
// A SEPARATE MODULE FROM index.ts on purpose: the barrel re-exports ClaudeChat,
// and a host reading the flag through it would pull the whole chat (and the
// markdown chunk) into its bundle for one boolean.
import { useEffect, useState } from "react";
import { getPrefs } from "@platform/lib/api";
import { GATE_FALLBACK_MS } from "@platform/lib/clock";

let enabled: boolean | null = null;
let reading: Promise<void> | null = null;
let generation = 0;
const listeners = new Set<(v: boolean | null) => void>();

/**
 * THE SECOND SWITCH THIS ONE PREFS READ ANSWERS: `prefs.chat.recap`, the native
 * chat's "While you were away" fold (shell/prefs.py `chat_recap_enabled`).
 *
 * It rides HERE rather than in a module of its own for one reason: every chat
 * embed already causes exactly one `/api/prefs` GET through `read()` below, and
 * a second reader would double that — six mounts on the tasks wall is six
 * needless round trips for one boolean.
 *
 * NOT a tri-state, unlike `enabled`. The argument for `null` up there is that a
 * premature `false` MOUNTS the wrong implementation; nothing mounts on this
 * one. It gates a fetch that cannot happen until the reader has been away a
 * minute, by which time the read has long landed — and the pref DEFAULTS ON, so
 * "not asked yet" and "on" are the same answer.
 */
let recap = true;
const recapListeners = new Set<(v: boolean) => void>();

/**
 * THE THIRD SWITCH THIS ONE PREFS READ ANSWERS: `prefs.queue.enabled`, the
 * project queue — one task in progress per folder (shell/prefs.py
 * `project_queue_enabled`).
 *
 * Here rather than in a module of its own for `recap`'s reason: every chat
 * embed already causes exactly one `/api/prefs` GET, and the composer asks this
 * on the keystroke that sends — a second reader would be a second round trip
 * per mount for one boolean.
 *
 * NOT a tri-state, and the argument is the opposite of the native flag's. `null`
 * up there exists because a premature `false` MOUNTS the wrong implementation;
 * this one mounts nothing. It gates ONE extra call in front of a send, it
 * DEFAULTS OFF, and off is exactly what every send did before the feature
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

function set(next: boolean | null) {
  if (enabled === next) return;
  enabled = next;
  for (const listener of listeners) listener(next);
}

function setRecap(next: boolean) {
  if (recap === next) return;
  recap = next;
  for (const listener of recapListeners) listener(next);
}

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
 * a paused process — where the promise simply never settles. `enabled` then sits
 * at `null` for the life of the page, and `null` is the state every chat MOUNT
 * holds a placeholder over: no iframe, no chat, no error, for ever.
 *
 * So the read is raced with the same 8 s backstop every other gate in this app
 * has (`platform/lib/clock.GATE_FALLBACK_MS`, `ChatFrame`'s original). Losing
 * the race is treated exactly as a failed read is — `false`, the default the
 * pref itself has — and `reading` is cleared either way, so the next mount asks
 * again rather than inheriting a verdict taken while the server was away.
 */
/** The budget itself, as a variable only so a test can make it small: eight
 *  seconds of real time per case is not a test anyone runs. Production never
 *  moves it — `setPrefsDeadlineForTests` is the only writer, and
 *  `resetNativeChatFlagForTests` puts it back. */
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
  // seconds of placeholder over every chat embed on the page, twice the number
  // this constant names and twice what `ChatFrame` gives the frame beside it.
  // Worse, the first attempt was ABANDONED at 8 s, so a GET that landed at 8.1 s
  // — which is exactly what a slow cold start looks like — was thrown away in
  // favour of a fresh request, and if that one also ran out the answer settled
  // on `false` and every embed on the page mounted legacy over a server whose
  // real answer had already arrived.
  //
  // So the budget goes around BOTH attempts. The retry is still there and still
  // worth one round trip — a prefs GET that REJECTS is usually a single dropped
  // request racing the server's start, and that rejection is fast — but it now
  // spends what is left of the eight seconds rather than opening a second
  // eight-second window, and a read that is merely slow is never given up on in
  // favour of asking again.
  reading = withDeadline(
    getPrefs().catch(() => getPrefs()),
    prefsDeadlineMs,
  )
    .then((p) => {
      if (generation !== departed) return;
      set(p.chat?.native === true);
      // `!== false`, never `=== true`: the recap is ON by default, so a server
      // that predates the field (or one whose prefs.json has never been
      // written) must read as on rather than silently losing the feature.
      setRecap(p.chat?.recap !== false);
      // `=== true`, the opposite polarity from the recap above: this one is
      // OPT-IN, so a server with no such field is a server whose sends are not
      // admitted through anything.
      setQueue(p.queue?.enabled === true);
    })
    .catch(() => {
      // STILL NO ANSWER — so `false`, not `null`. `null` is "not asked yet" and
      // every MOUNT holds a placeholder over it (ChatMount), so leaving it
      // there after a failed read turns every chat embed on the page into a
      // permanent skeleton: no iframe, no chat, no error. Legacy is what a
      // server we cannot ask about `chat.native` is running today, it is the
      // default the pref itself has, and it is what these sites rendered before
      // the flag existed — never-broken outranks the tri-state. `reading` is
      // cleared so a later mount (or a publish) can still ask again.
      if (generation !== departed) return;
      // ONLY A FIRST READ SETTLES ON `false` (review, 2026-09-16). A re-read —
      // a tab coming back into view — that fails keeps the answer the page
      // already has: `set(false)` here remounted every live native chat as the
      // legacy iframe on one refused GET after a laptop wake. And `reading` is
      // cleared only by the read that owns it, never by a superseded one.
      if (!settledOnce) set(false);
      reading = null;
    })
    .then(() => {
      if (generation === departed) settledOnce = true;
    });
  return reading;
}

/** Hand over a known-fresh answer for the recap switch (the prefs payload a PUT
 *  returned). No `generation` bump: this value is not what `read()` retries for,
 *  and taking the native flag's answer away would put every mount back on a
 *  skeleton for a click that was not about it. */
export function publishChatRecapEnabled(next: boolean) {
  setRecap(next);
}

/** Hand over a known-fresh answer for the project queue (the prefs payload a
 *  PUT returned). No `generation` bump, for `publishChatRecapEnabled`'s reason:
 *  this is not the value `read()` retries for, and taking the native flag's
 *  answer away would put every mount back on a skeleton for a click that was
 *  not about it. */
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
  // A NEW GENERATION, so the read this replaces cannot speak after it (Bugbot):
  // without the bump an older GET that later timed out still matched
  // `generation`, called `set(false)`, and remounted every native chat embed as
  // the legacy iframe over a newer read that had already succeeded.
  generation += 1;
  reading = null;
  return read();
}

/** The `localStorage` key a Settings toggle announces itself on, so every
 *  OTHER tab of this app hears the flip through the `storage` event instead of
 *  keeping the answer it read at load. */
export const QUEUE_FLAG_BROADCAST_KEY = "fused-render:project-queue";

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

/** Subscribe to the project queue switch. Triggers the same one prefs read the
 *  native flag uses, so a chat that is already mounted pays nothing for asking. */
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

/** Whether the session-recap fold is offered. Defaults ON — see `recap`. */
export function chatRecapEnabledNow(): boolean {
  return recap;
}

/** Subscribe to the recap switch. Triggers the same one prefs read the native
 *  flag uses, so a chat that is already mounted pays nothing for asking. */
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

/** Hand over a known-fresh answer (the prefs payload a PUT returned). */
export function publishNativeChatEnabled(next: boolean) {
  generation += 1;
  reading = Promise.resolve();
  set(next);
}

/** Current answer without subscribing; `null` until the first read lands. */
export function nativeChatEnabledNow(): boolean | null {
  return enabled;
}

/** Test-only: forget the cached answer so a suite starts from "not asked".
 *  NOTIFIES, like every other write: a component already mounted would
 *  otherwise keep the answer the suite just took away. */
export function resetNativeChatFlagForTests() {
  reading = null;
  settledOnce = false;
  generation += 1;
  prefsDeadlineMs = GATE_FALLBACK_MS;
  set(null);
  setRecap(true);
  setQueue(false);
}

/**
 * Subscribe, tri-state: `null` until the one prefs read lands. THE HOOK A MOUNT
 * USES — see the header for why "not asked yet" may not render either branch.
 */
export function useNativeChatFlag(): boolean | null {
  const [current, setCurrent] = useState<boolean | null>(nativeChatEnabledNow);
  useEffect(() => {
    listeners.add(setCurrent);
    setCurrent(nativeChatEnabledNow());
    void read();
    return () => {
      listeners.delete(setCurrent);
    };
  }, []);
  return current;
}

/** The same subscription, flattened to "is the native chat on RIGHT NOW". For a
 *  host's side question only (a param-boundary flag, an ask ledger): those want
 *  a boolean and "not asked yet" is honestly "no" for them. */
export function useNativeChatEnabled(): boolean {
  return useNativeChatFlag() === true;
}
