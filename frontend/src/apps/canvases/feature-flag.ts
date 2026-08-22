// Whether Canvases is OFFERED on this machine — the `canvases_enabled`
// preference (D427, shell/prefs.py), read by the shell rather than by the
// canvases app: the sidebar shows its row and the Settings menu its entry only
// once someone has turned the feature on. Default off.
//
// A SEPARATE MODULE FROM index.ts, for the reason logged-in.ts spells out: the
// barrel re-exports Canvases/CanvasWorkspace, so a sidebar that read the flag
// through it would pull the whole canvases app — workspace, lock lib, embed
// host — into the shell's main bundle for one boolean. Import this file
// directly and the app itself stays lazy behind its route.
//
// A SEPARATE FACT FROM logged-in.ts, too, and deliberately not folded into it:
// "is this feature offered" and "is there an account behind it" answer
// different questions, and only the primary nav row wants both (the Settings
// menu entry is gated on this one alone — it has always been the affordance for
// "there is a thing here you could set up"). The call site ANDs them.
//
// ONE FETCH, NOT A POLL — the difference from logged-in.ts. A login happens
// elsewhere (another window, `fused login` in a terminal) and so has to be
// watched for; this pref can only change on the Preferences page of this app,
// and that page PUBLISHES its new value, so the row appears or disappears the
// moment the checkbox settles. A hand-edited prefs.json is picked up on the
// next load, which is the same deal every other pref on that page offers.
import { useEffect, useState } from "react";
import { getPrefs } from "@platform/lib/api";

/** The last answer, or `null` while nobody has asked yet. Null renders as OFF:
 *  the flag defaults off, so hiding until the answer lands never flashes an
 *  entry the reader turned off, whereas assuming on would. */
let enabled: boolean | null = null;
/** The in-flight read, shared — the sidebar remounts on every navigation (App
 *  keys it on the nav epoch), and a fresh GET per trip is waste when the value
 *  cannot change without this module being told. */
let reading: Promise<void> | null = null;
const listeners = new Set<(v: boolean) => void>();

function set(next: boolean) {
  if (enabled === next) return;
  enabled = next;
  for (const listener of listeners) listener(next);
}

function read(): Promise<void> {
  if (reading) return reading;
  reading = getPrefs()
    .then((p) => set(p.canvases.enabled))
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
  // The cached read is now the stale one — mark it done so a later mount does
  // not overwrite a published value with an older GET's.
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
