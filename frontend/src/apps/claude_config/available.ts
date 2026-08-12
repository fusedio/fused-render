// Whether the Claude Config bridge is installed on this machine.
//
// Split out of index.ts on purpose: index.ts also re-exports the heavy
// `ClaudeConfig` panel (ClaudeConfig.tsx + its sections/bits/ansi
// dependencies), and shell/ShellSidebar.tsx needs this probe on every route to
// decide whether to show the CLAUDE sidebar entries at all — eagerly, not
// behind the `/claude-config` route. If that eager check imported the hook
// through the barrel, the barrel's static re-export of `ClaudeConfig` would
// pull the whole settings panel into the shell's main bundle even though it
// only renders once the user actually opens `/claude-config` (shell/App.tsx
// lazy-loads it). Importing straight from this module keeps the probe eager
// and the panel itself lazy.
import { useEffect, useState } from "react";
import { getStatus } from "./api";

// Module-level cache of a CONFIRMED answer, shared by every mount of the hook.
// The shell remounts every route on each navigation (App.tsx keys them on the
// nav epoch) and the sidebar re-renders with them, so without this the two
// CLAUDE entries would blink out and back on every trip through the app.
//
// Deliberately simpler than @platform/lib/hooks' useBuiltinMountReady: that one
// polls, because a mount becomes ready asynchronously minutes into a session.
// Availability here is a property of the server's own installation — the
// bridge's modules import or they don't — so it cannot flip mid-session and one
// fetch is the whole story.
let cached: boolean | null = null;

export function useClaudeConfigAvailable(): boolean {
  const [available, setAvailable] = useState(cached ?? false);
  useEffect(() => {
    if (cached !== null) return;
    let alive = true;
    getStatus().then(
      (s) => {
        cached = s.available;
        if (alive) setAvailable(s.available);
      },
      () => {
        // A failed probe is not a cached "no": a transient fetch failure would
        // otherwise hide the sidebar entries for the rest of the session.
        if (alive) setAvailable(false);
      },
    );
    return () => {
      alive = false;
    };
  }, []);
  return available;
}
