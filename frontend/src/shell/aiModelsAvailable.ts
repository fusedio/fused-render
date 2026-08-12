// Sidebar gate. Availability is "does the hub cache dir exist", which — unlike
// the Claude config bridge's install-shaped answer — CAN flip mid-session: the
// first model a user ever downloads creates it. So a confirmed `true` is cached
// for the session (the row must not blink out when the shell swaps sidebars),
// while a `false` is only cached for PROBE_TTL_MS and re-probed by the next
// mount after it lapses. That bounds the cost to roughly one isdir() a minute
// for the majority of machines that have no cache at all.
//
// The answer is PUBLISHED rather than just stored, because the two writers are
// not the mounted sidebar: a probe from another mount, and the page's own load
// (which knows the truth without a second request), both have to reach a
// sidebar that is already on screen. Without that, opening /ai-models by URL
// on a machine whose cache appeared this session would update the cache and
// leave the entry missing until something remounted the sidebar. Deleting the
// last repo publishes too — the cache DIRECTORY survives an empty cache, so the
// entry stays, which is correct: the page still has a true thing to say.
//
// Split out of AiModels.tsx (the heavy /ai-models page) on purpose: ShellSidebar
// needs this probe eagerly, on every route, and AiModels.tsx is lazy-loaded
// (shell/App.tsx) — importing the hook through the page's own module would drag
// the whole page back into the shell's main bundle. `publishAvailable` is
// exported so the page itself (the OTHER writer, on its own load and on
// deleting the last repo) can still publish through the same cache/listener
// set once it's mounted.
import { useEffect, useState } from "react";
import { getAiModelsStatus } from "@platform/lib/api";

const PROBE_TTL_MS = 60_000;
let cached: { available: boolean; at: number } | null = null;
const gateListeners = new Set<(available: boolean) => void>();

export function publishAvailable(available: boolean) {
  cached = { available, at: Date.now() };
  for (const listener of gateListeners) listener(available);
}

export function useAiModelsAvailable(): boolean {
  const [available, setAvailable] = useState(cached?.available ?? false);
  useEffect(() => {
    gateListeners.add(setAvailable);
    // An answer that landed between this render and this effect (another
    // mount's probe resolving) would otherwise be missed.
    if (cached) setAvailable(cached.available);
    if (!cached || (!cached.available && Date.now() - cached.at >= PROBE_TTL_MS)) {
      getAiModelsStatus().then(
        (s) => publishAvailable(s.available),
        () => {
          // A failed probe is not a cached "no": leave the last known answer
          // (and the absent cache entry) alone so a transient fetch failure
          // neither hides a shown entry nor suppresses the next probe.
        },
      );
    }
    return () => {
      gateListeners.delete(setAvailable);
    };
  }, []);
  return available;
}
