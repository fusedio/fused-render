// Whether the unified Share sheet is OFFERED on this machine — the
// `app_sharing_enabled` preference (shell/prefs.py), read by every surface
// that lets a reader take an app out: the /apps card's hover chip and
// right-click menu, the app page header, the explorer kebab, the explorer
// folder row. Default off.
//
// ON, each surface shows ONE "Share" entry and the sheet behind it
// (ShareAppModal) holds both routes — the public link and the `.fused` file.
// OFF, each surface shows the plain Export / Download action it carried before
// the sheet existed (PR #1207): the `.fused` lands in Downloads and a toast
// says where. `exportAppFileOnly` in share-app.ts is that action; nothing else
// opens the sheet, so the link route is unreachable while this is off.
//
// Same shape as @apps/canvases/feature-flag — ONE FETCH, NOT A POLL: the pref
// can only change on this app's Preferences page, and that page PUBLISHES the
// new value, so every mounted surface flips with the checkbox. The
// `generation` counter is load-bearing for the same reason it is there:
// reassigning `reading` does not cancel a GET already in flight, and without
// the bump a pre-toggle payload landing after the publish would write the old
// value back over the fresh one.
//
// One addition the canvases flag does not need: a SYNCHRONOUS getter. The
// /apps card's right-click menu (appCardMenu) is a plain function built at
// click time, not a component, so it cannot subscribe — it asks the module for
// its last answer instead. `null` (not asked yet) reads as OFF, which is the
// pref's own default, so the worst a click inside the first read's window can
// do is behave like a machine that never turned the feature on.
import { useEffect, useState } from "react";
import { getPrefs } from "@platform/lib/api";

/** The last answer, or `null` while nobody has asked yet. Null renders as OFF:
 *  the flag defaults off, so showing Export until the answer lands never
 *  flashes a Share button the reader has not turned on. */
let enabled: boolean | null = null;
let reading: Promise<void> | null = null;
let generation = 0;
const listeners = new Set<(v: boolean) => void>();

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
      if (generation === departed) set(p.app_sharing?.enabled === true);
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
export function publishAppSharingEnabled(next: boolean) {
  // Bump FIRST so a read already in flight drops its (stale) answer.
  generation += 1;
  reading = Promise.resolve();
  set(next);
}

/** The last answer, synchronously — for the one caller that is not a
 *  component (appCardMenu). Off until a read has landed. */
export function appSharingEnabled(): boolean {
  void read();
  return enabled === true;
}

/** Subscribe. The first reader triggers the one read; later ones reuse it. */
export function useAppSharingFeature(): boolean {
  const [current, setCurrent] = useState(enabled === true);
  useEffect(() => {
    listeners.add(setCurrent);
    setCurrent(enabled === true);
    void read();
    return () => {
      listeners.delete(setCurrent);
    };
  }, []);
  return current;
}
