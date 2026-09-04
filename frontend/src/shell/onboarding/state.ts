// The first-run wizard is a ROUTE (`/onboarding`), not an overlay: App.tsx
// renders it alone (no sidebar, no status bar) on that path, the server
// answers a refresh on it (server/routers/shell.py), and Help › Setup wizard
// is an ordinary link. Opening and closing are navigations — nothing here
// needs a store. What is left is the boot-time rule, and the resume point.
import type { Config } from "@platform/lib/api";

export const ONBOARDING_PATH = "/onboarding";

/** The auto-show rule, from the server's flag: never completed AND never
    dismissed. A backend without the field (older server) shows nothing. */
export function shouldAutoShow(config: Config): boolean {
  const s = config.onboarding;
  if (!s) return false;
  return s.completed_at == null && s.dismissed_at == null;
}

// The resume point. The server keeps the last open step (shell/onboarding.py
// `step`) so a restart lands on it; but `config` is fetched ONCE at boot
// (main.tsx), so within a page load the wizard's own writes would be invisible
// to a reopen — dismiss on step 3, Help › Setup wizard, and the prop still
// says whatever boot said. This module-level memory is the same-page-load
// half; the server is the across-restart half. Not localStorage: every port
// is a new origin (see the server module's docstring).
//
// `undefined` = this page load has not touched the wizard (defer to boot);
// `null` = it completed the wizard (start over, as the server also says now).
let rememberedStep: string | null | undefined = undefined;

export function rememberStep(step: string | null): void {
  rememberedStep = step;
}

/** Where a reopen should land, most recent source first: this page load's
    memory, then the boot snapshot. Null when neither knows. */
export function recallStep(config: Config): string | null {
  if (rememberedStep !== undefined) return rememberedStep;
  return config.onboarding?.step ?? null;
}

/** The wizard's URL for the boot auto-show: names the resume step when the
    server has one, so a restart mid-wizard reopens the same page. */
export function onboardingUrl(config: Config): string {
  const step = recallStep(config);
  return step ? `${ONBOARDING_PATH}?step=${encodeURIComponent(step)}` : ONBOARDING_PATH;
}
