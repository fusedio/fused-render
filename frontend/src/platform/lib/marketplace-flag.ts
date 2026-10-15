// Whether the Marketplace is OFFERED on this machine — the
// `marketplace_enabled` preference (shell/prefs.py), read by the two entry
// points: the sidebar's Marketplace row (shell/GlobalSidebar.tsx, both the
// expanded list and the collapsed rail) and the shell's `/marketplace` routes
// (App.tsx). Default off.
//
// ON, the row renders after AI Models and `/marketplace/*` mounts the store
// (apps/marketplace). OFF, the row is not rendered at all and `/marketplace`
// shows a one-line notice pointing at Preferences. The /api/marketplace
// endpoints themselves are not gated — the flag gates entry points, like
// every other feature flag.
//
// Copied from monitor-flag.ts, same shape — ONE FETCH, NOT A POLL: the pref
// can only change on this app's Preferences page, and that page PUBLISHES the
// new value, so the row appears or disappears with the checkbox. The
// `generation` counter is load-bearing for the same reason: a pre-toggle GET
// landing after the publish must not write the old value back over the fresh
// one. No postMessage to a framing parent (monitor-flag's framed-Preferences
// case): the only framed Preferences is the Bots page's panel, and the Fused
// Bot flavor offers no Marketplace at all.
import { useEffect, useState } from "react";
import { getPrefsShared } from "@platform/lib/api";

/** The last answer, or `null` while nobody has asked yet. The sidebar treats
 *  null as OFF (the default, so nothing flashes in before the answer lands);
 *  the /marketplace route treats it as "hold" and paints its loading
 *  fallback, so a reader with the pref ON who reloads the page never sees the
 *  "Marketplace is off" notice for the one round-trip the read takes. */
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
      if (generation === departed) set(p.marketplace?.enabled === true);
    })
    .catch(() => {
      // A failed read is not an answer: leave `enabled` as it was and retry
      // on a timer while someone is still listening (the route holds on
      // `null`, and App never remounts) — see monitor-flag.ts.
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
export function publishMarketplaceEnabled(next: boolean) {
  // Bump FIRST so a read already in flight drops its (stale) answer.
  generation += 1;
  reading = Promise.resolve();
  set(next);
}

/** Subscribe. `null` until the one shared read lands, `true`/`false` after.
 *  The first reader triggers the read; later ones reuse it. */
export function useMarketplaceFeature(): boolean | null {
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
