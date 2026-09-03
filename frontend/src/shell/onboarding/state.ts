// The first-run wizard is a ROUTE (`/onboarding`), not an overlay: App.tsx
// renders it alone (no sidebar, no status bar) on that path, the server
// answers a refresh on it (server/routers/shell.py), and Help › Setup wizard
// is an ordinary link. Opening and closing are navigations — nothing here
// needs a store. What is left is the one boot-time rule.
import type { Config } from "@platform/lib/api";

export const ONBOARDING_PATH = "/onboarding";

/** The auto-show rule, from the server's flag: never completed AND never
    dismissed. A backend without the field (older server) shows nothing. */
export function shouldAutoShow(config: Config): boolean {
  const s = config.onboarding;
  if (!s) return false;
  return s.completed_at == null && s.dismissed_at == null;
}
