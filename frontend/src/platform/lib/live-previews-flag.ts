// Whether card thumbnails may render the LIVE app — the `live_previews_enabled`
// preference (shell/prefs.py), read by the two places that build a scaled
// thumbnail iframe: the /apps hub card (AppPreviewCard: the live body of a
// card with no preview.png, and the hover swap on a card that has one) and
// the explorer's bookmark / recent / folder-peek cards (BookmarkCards'
// LivePreview). Default OFF — opt-in.
//
// OFF, no thumbnail iframe mounts anywhere: a still-thumbed card keeps its
// still and ignores hover, and a card with nothing authored shows the
// placeholder mark (ThumbPlaceholder) instead of the empty box or the app.
//
// Same shape as share-app-flag.ts — ONE FETCH, NOT A POLL, with the
// `generation` counter for the same in-flight-read race. The store is
// tri-state internally (`null` = nobody has asked yet) but the hook hands
// out a plain boolean: "not asked yet" renders as the default, OFF, exactly
// like the other opt-in flags. While this defaulted ON the hook returned the
// `null` too and the cards held an empty box until the read landed, because
// guessing on would have booted every live iframe for a reader who had
// turned them off; with off as the default the only cost of guessing is one
// placeholder→iframe swap for a reader who opted IN, one local round-trip
// after mount and shorter than the preview scheduler's idle wait anyway.
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
      // `=== true`: opt-in, so a server that predates the field reads as
      // off — the same rule the server applies to a prefs file without the key.
      if (generation === departed) set(p.live_previews?.enabled === true);
    })
    .catch(() => {
      // A failed read is not an answer. Fall back to the default (off) so the
      // cards stop holding, and let the next mount try again.
      reading = null;
      if (generation === departed && enabled === null) set(false);
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

/** Subscribe. Off until the one shared read lands (see the module comment),
 *  then the stored answer. */
export function useLivePreviewsFeature(): boolean {
  const [current, setCurrent] = useState<boolean | null>(enabled);
  useEffect(() => {
    listeners.add(setCurrent);
    setCurrent(enabled);
    void read();
    return () => {
      listeners.delete(setCurrent);
    };
  }, []);
  return current ?? false;
}
