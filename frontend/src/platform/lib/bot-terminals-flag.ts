// Whether bots may drive their own terminal on this machine — the
// `bot_terminals_enabled` preference (shell/prefs.py), the global half of two
// switches (a bot also needs its own Terminal setting). Read by the Bots
// settings dialog, which only offers the per-bot Terminal row while this is
// on. Default off.
//
// Copied from marketplace-flag.ts, same shape — ONE FETCH, NOT A POLL: the
// pref can only change on this app's Preferences page, and that page
// PUBLISHES the new value. The `generation` counter is load-bearing for the
// same reason: a pre-toggle GET landing after the publish must not write the
// old value back over the fresh one.
import { useEffect, useState } from "react";
import { getPrefsShared } from "@platform/lib/api";

/** The last answer, or `null` while nobody has asked yet (treated as OFF). */
let enabled: boolean | null = null;
let reading: Promise<void> | null = null;
let generation = 0;
/** How long a failed read waits before the next try. */
const RETRY_MS = 3000;
const listeners = new Set<(v: boolean | null) => void>();

function set(next: boolean) {
  if (enabled === next) return;
  enabled = next;
  for (const listener of listeners) listener(next);
}

function read(): Promise<void> {
  if (reading) return reading;
  const departed = generation;
  reading = getPrefsShared() // single-flight across flag modules (api.ts)
    .then((p) => {
      // `=== true`: opt-in, so a server that predates the field reads as off.
      if (generation === departed) set(p.bot_terminals?.enabled === true);
    })
    .catch(() => {
      reading = null;
      setTimeout(() => {
        if (enabled === null && listeners.size > 0) void read();
      }, RETRY_MS);
    })
    .then(() => {});
  return reading;
}

/** Hand over a known-fresh answer — the prefs payload a PUT returned. Called
 *  by the Preferences page's toggle. */
export function publishBotTerminalsEnabled(next: boolean) {
  // Bump FIRST so a read already in flight drops its (stale) answer.
  generation += 1;
  reading = Promise.resolve();
  set(next);
}

/** Subscribe. `null` until the one shared read lands, `true`/`false` after. */
export function useBotTerminalsFeature(): boolean | null {
  const [current, setCurrent] = useState<boolean | null>(enabled);
  useEffect(() => {
    listeners.add(setCurrent);
    setCurrent(enabled);
    void read();
    return () => {
      listeners.delete(setCurrent);
    };
  }, []);
  return current;
}
