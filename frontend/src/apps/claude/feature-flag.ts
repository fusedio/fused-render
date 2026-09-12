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

function read(): Promise<void> {
  if (reading) return reading;
  const departed = generation;
  reading = getPrefs()
    // ONE BOUNDED RETRY, then a real answer either way. A prefs GET that fails
    // is usually a single dropped request (a reload racing the server's start),
    // so asking twice is worth one round trip.
    .catch(() => getPrefs())
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
      if (generation === departed) set(false);
      reading = null;
    })
    .then(() => {});
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
export function publishProjectQueueEnabled(next: boolean) {
  setQueue(next);
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
  generation += 1;
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
