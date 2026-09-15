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

function read(): Promise<void> {
  if (reading) return reading;
  const departed = generation;
  reading = getPrefs()
    // ONE BOUNDED RETRY, then whatever we have. A prefs GET that fails is
    // usually a single dropped request (a reload racing the server's start), so
    // asking twice is worth one round trip.
    .catch(() => getPrefs())
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
  setRecap(true);
}
