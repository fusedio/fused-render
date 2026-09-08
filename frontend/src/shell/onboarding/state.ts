// The first-run wizard is a ROUTE (`/onboarding`), not an overlay: App.tsx
// renders it alone (no sidebar, no status bar) on that path, the server
// answers a refresh on it (server/routers/shell.py), and Help › Setup wizard
// is an ordinary link. Opening and closing are navigations — nothing here
// needs a store. What is left is the boot-time rule.
//
// There is no "resume at the last open step" any more (it was stored
// server-side as `step`): where the wizard opens is the FIRST STEP STILL TO DO,
// read off the stage statuses — progress.ts `onboardingUrl`. What is left is a
// better answer than where the user last happened to be.
import type { Config } from "@platform/lib/api";

export const ONBOARDING_PATH = "/onboarding";

/** The auto-show rule, from the server's flags: never completed, never
    dismissed AND NEVER OPENED. The wizard is for a first run; once it has been
    on screen, however the user left it (Back, a refresh, the sidebar meter),
    /home is /home again and the meter row is the way back in. A backend without
    the field (older server) shows nothing. */
export function shouldAutoShow(config: Config): boolean {
  const s = config.onboarding;
  if (!s) return false;
  return s.completed_at == null && s.dismissed_at == null && s.opened_at == null;
}
