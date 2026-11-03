// Whether Canvases is OFFERED on this machine — the `canvases_enabled`
// preference (D427, shell/prefs.py), read by the shell rather than by the
// canvases app: the sidebar shows its Canvases section and the Settings menu
// its entry only once someone has turned the feature on. Default off.
//
// THE ONLY GATE (owner, 2026-10-22). It used to be ANDed with a second store,
// "is there an account behind it" (logged-in.ts, D358), which on a new machine
// meant turning the feature on showed nothing until a sign-in had happened
// somewhere. The section now appears with the switch; its "+ Canvas" row
// opens the catalog, which explains a CLI-missing / signed-out state itself
// and is where sign-in happens. The account store is gone with its last
// reader.
//
// A SEPARATE MODULE FROM index.ts (the reason glyph.tsx is split out too): the
// barrel re-exports Canvases/CanvasWorkspace, so a sidebar that read the flag
// through it would pull the whole canvases app — workspace, embed
// host — into the shell's main bundle for one boolean. Import this file
// directly and the app itself stays lazy behind its route.
//
// ONE FETCH, NOT A POLL. This pref can only change on the Preferences page of
// this app, and that page PUBLISHES its new value, so the section appears or
// disappears the moment the checkbox settles. A hand-edited prefs.json is
// picked up on the next load, which is the same deal every other pref on that
// page offers.
import { useEffect, useState } from "react";
import { getPrefsShared } from "@platform/lib/api";

/** The last answer, or `null` while nobody has asked yet. Null renders as OFF:
 *  the flag defaults off, so hiding until the answer lands never flashes an
 *  entry the reader turned off, whereas assuming on would. */
let enabled: boolean | null = null;
/** The in-flight read, shared — the sidebar remounts on every navigation (App
 *  keys it on the nav epoch), and a fresh GET per trip is waste when the value
 *  cannot change without this module being told. */
let reading: Promise<void> | null = null;
/** Which answer is newest: a publish bumps
 *  it, and a read that DEPARTED earlier drops its result rather than
 *  overwriting the fresher one. Nothing else can cancel an in-flight GET — a
 *  reader who lands on /preferences and flips the checkbox before that GET
 *  resolves would otherwise watch the row they just enabled vanish when the
 *  pre-toggle payload arrives. */
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
  reading = getPrefsShared() // single-flight across flag modules (api.ts)
    .then((p) => {
      if (generation === departed) set(p.canvases.enabled);
    })
    .catch(() => {
      // A failed read is not an answer: leave `enabled` as it was (off, the
      // default, on a first load) and let the next mount try again rather than
      // pinning "off" for the session because one GET lost a race with a
      // server restart.
      reading = null;
    })
    .then(() => {});
  return reading;
}

/**
 * Hand over a known-fresh answer — the prefs payload a PUT returned.
 *
 * Called by the Preferences page's toggle, which already has the whole updated
 * `Prefs` in hand: the sidebar is mounted beside it, so without this the reader
 * flips the checkbox and the row they just enabled does not appear until they
 * navigate.
 */
export function publishCanvasesEnabled(next: boolean) {
  // Bump FIRST: a read already in flight departed on the old generation and
  // will now drop its answer instead of writing the pre-toggle value over this
  // one. Reassigning `reading` alone would not have done it — that only stops a
  // later mount from starting another GET, and the chain already running keeps
  // its `.then`.
  generation += 1;
  reading = Promise.resolve();
  set(next);
}

/** Subscribe. The first reader triggers the one read; later ones reuse it. */
export function useCanvasesFeature(): boolean {
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
