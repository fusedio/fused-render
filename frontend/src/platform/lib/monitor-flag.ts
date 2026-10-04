// Whether the process Monitor is OFFERED on this machine — the
// `monitor_enabled` preference (shell/prefs.py), read by the two entry points:
// the status bar's System chip (shell/SystemDock.tsx, whose popover holds
// "Open Monitor") and the shell's `/monitor` route (App.tsx). Default off.
//
// ON, the chip renders at the bar's left edge and `/monitor` mounts the
// process monitor page. OFF, the chip is not rendered at all (so nothing
// polls /api/system/activity and the sampler never wakes) and `/monitor`
// shows a one-line notice pointing at Preferences.
//
// Same shape as share-app-flag.ts — ONE FETCH, NOT A POLL: the pref can only
// change on this app's Preferences page, and that page PUBLISHES the new
// value, so the chip appears or disappears with the checkbox. The `generation`
// counter is load-bearing for the same reason: a pre-toggle GET landing after
// the publish must not write the old value back over the fresh one.
import { useEffect, useState } from "react";
import { getPrefs } from "@platform/lib/api";

/** The last answer, or `null` while nobody has asked yet. The chip treats
 *  null as OFF (the default, so nothing flashes in before the answer lands);
 *  the /monitor route treats it as "hold" and paints its loading fallback,
 *  because a reader with the pref ON who reloads /monitor must not see the
 *  "Monitor is off" notice for the one round-trip the read takes. */
let enabled: boolean | null = null;
let reading: Promise<void> | null = null;
let generation = 0;
const listeners = new Set<(v: boolean | null) => void>();

function set(next: boolean) {
  if (enabled === next) return;
  enabled = next;
  for (const listener of listeners) listener(next);
}

function read(): Promise<void> {
  if (reading) return reading;
  const departed = generation;
  reading = getPrefs()
    .then((p) => {
      // `=== true`: opt-in, so a server that predates the field reads as off.
      if (generation === departed) set(p.monitor?.enabled === true);
    })
    .catch(() => {
      // A failed read is not an answer: leave `enabled` as it was and let the
      // next mount try again rather than pinning "off" for the session.
      reading = null;
    })
    .then(() => {});
  return reading;
}

/** Hand over a known-fresh answer — the prefs payload a PUT returned. Called
 *  by the Preferences page's toggle. */
export function publishMonitorEnabled(next: boolean) {
  // Bump FIRST so a read already in flight drops its (stale) answer.
  generation += 1;
  reading = Promise.resolve();
  set(next);
}

/** Subscribe. `null` until the one shared read lands, `true`/`false` after.
 *  The first reader triggers the read; later ones reuse it. */
export function useMonitorFeature(): boolean | null {
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
