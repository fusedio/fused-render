// Whether the Share sheet (ShareAppModal) offers its GitHub link card — the
// `github_link_enabled` preference (shell/prefs.py). Default off.
//
// ON, the sheet shows a third card under the public link and the file: a
// udf.ai page that installs the app from its GitHub repo, copyable once the
// folder is its own repo with nothing unpushed (share_app.py
// `github_link_state` decides). OFF, the card is not rendered. The sheet
// itself still sits behind `app_sharing_enabled` (share-app-flag.ts).
//
// Copied from marketplace-flag.ts, same shape — ONE FETCH, NOT A POLL: the
// pref can only change on this app's Preferences page, and that page
// PUBLISHES the new value. The `generation` counter keeps a pre-toggle GET
// landing after the publish from writing the old value back.
import { useEffect, useState } from "react";
import { getPrefsShared } from "@platform/lib/api";

/** The last answer, or `null` while nobody has asked yet. Null renders as OFF
 *  (the default), so the card never flashes in before the answer lands. */
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
  reading = getPrefsShared() // single-flight across flag modules (api.ts)
    .then((p) => {
      // `=== true`: opt-in, so a server that predates the field reads as off.
      if (generation === departed) set(p.github_link?.enabled === true);
    })
    .catch(() => {
      // A failed read is not an answer: leave `enabled` as it was and let the
      // next mount try again.
      reading = null;
    })
    .then(() => {});
  return reading;
}

/** Hand over a known-fresh answer — the prefs payload a PUT returned. Called
 *  by the Preferences page's toggle. */
export function publishGithubLinkEnabled(next: boolean) {
  // Bump FIRST so a read already in flight drops its (stale) answer.
  generation += 1;
  reading = Promise.resolve();
  set(next);
}

/** Subscribe. The first reader triggers the one read; later ones reuse it. */
export function useGithubLinkFeature(): boolean {
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
