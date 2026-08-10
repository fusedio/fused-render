// Claude Config app entry point.
//
// Unlike learn and sessions, this app is NOT html+py content in a mount: it is
// native React (ClaudeConfig.tsx) over a dedicated server bridge
// (POST /api/claude-config/{module}, see ./api.ts). It went native so it could
// use the shell's palette tokens, toast surface and modal chassis — an iframe'd
// html app carried its own Claude-branded dark theme and ignored the app's
// Light/Dark setting entirely.
//
// The shell gives it one sidebar route under the CLAUDE heading, gated on
// `useClaudeConfigAvailable` (shell/ShellSidebar.tsx, dispatched in
// shell/App.tsx): `/claude-config`, the settings panel, whose "MD Files"
// section is the CLAUDE.md explorer (`?cctab=claudemd`; the old `/claude-md`
// page redirects there). It briefly also hung off the Preferences page as a
// tab; a settings page with a second settings app nested inside one of its tabs
// was one surface too many.
// Mutable state (settings.json, the git history, MCP registrations) is written
// by the server-side modules to ~/.claude — this app owns none of it.
import { useEffect, useState } from "react";
import { getStatus } from "./api";

export { default as ClaudeConfig } from "./ClaudeConfig";

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
