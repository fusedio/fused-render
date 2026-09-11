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

function set(next: boolean | null) {
  if (enabled === next) return;
  enabled = next;
  for (const listener of listeners) listener(next);
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
      if (generation === departed) set(p.chat?.native === true);
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
