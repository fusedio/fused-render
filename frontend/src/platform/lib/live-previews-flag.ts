// Whether card thumbnails may render the LIVE app — the `live_previews_enabled`
// preference (shell/prefs.py), read by the two places that build a scaled
// thumbnail iframe: the /apps hub card (AppPreviewCard: the live body of a
// card with no preview.png, and the hover swap on a card that has one) and
// the explorer's bookmark / recent / folder-peek cards (BookmarkCards'
// LivePreview). Default ON.
//
// OFF, no thumbnail iframe mounts anywhere: a still-thumbed card keeps its
// still and ignores hover, and a card with nothing authored shows the
// placeholder mark (ThumbPlaceholder) instead of the empty box or the app.
//
// Same shape as share-app-flag.ts — ONE FETCH, NOT A POLL, with the
// `generation` counter for the same in-flight-read race — but the answer is
// TRI-STATE. The other flags default off, so "not asked yet" can safely render
// as off. This one defaults ON, and rendering `null` as on would boot every
// live iframe in view on each cold load of /apps for a reader who turned them
// off — the exact cost they opted out of — only to tear them all down when the
// GET lands. So `null` means "hold": the cards mount nothing until the one
// shared read answers, which is one local round-trip behind the grid's own
// data and shorter than the preview scheduler's idle wait anyway.
import { useEffect, useState } from "react";
import { getPrefs } from "@platform/lib/api";

/** The last answer, or `null` while nobody has asked yet. */
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
      // `!== false`: default on, so a server that predates the field reads as
      // on — the same rule the server applies to a prefs file without the key.
      if (generation === departed) set(p.live_previews?.enabled !== false);
    })
    .catch(() => {
      // A failed read is not an answer. Unlike the opt-in flags, holding at
      // `null` forever would hide every live thumbnail for the session over
      // one lost GET, so fall back to the default (on) and let the next mount
      // try again.
      reading = null;
      if (generation === departed && enabled === null) set(true);
    })
    .then(() => {});
  return reading;
}

/** Hand over a known-fresh answer — the prefs payload a PUT returned. Called
 *  by the Preferences page's toggle. */
export function publishLivePreviewsEnabled(next: boolean) {
  // Bump FIRST so a read already in flight drops its (stale) answer.
  generation += 1;
  reading = Promise.resolve();
  set(next);
}

/** Subscribe. `null` until the one shared read lands — callers mount nothing
 *  live while it is null (see the module comment); `true`/`false` after. */
export function useLivePreviewsFeature(): boolean | null {
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
