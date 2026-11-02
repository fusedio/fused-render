import type { ReactNode } from "react";
// Step 7 — the first app. The Home composer as-is (it names, scaffolds, starts
// the Claude run and navigates). That path is the wizard's "complete": this is
// the only step whose action writes anything, so an abandoned wizard leaves no
// junk. (Until 2026-10-29 the step also offered the showcase's local-AI apps,
// cloned at startup; the Community page is where a new user gets apps now.)
//
// `onComplete` writes the flag ONLY — it does not close the overlay. The
// wizard completes and closes on the route change the composer causes, so a
// composer `task_error` (folder made, Claude didn't start) stays readable in
// place.
import { AlertTriangle } from "lucide-react";

import { HeroComposer } from "@apps/builder/HomeHero";
import type { ClaudeHealth } from "@platform/lib/api";

import { StepHeader } from "./StepHeader";

export function FirstAppStep({
  health,
  eyebrow,
  onComplete,
}: {
  health: ClaudeHealth | null;
  eyebrow: ReactNode;
  /** The composer created a folder — real progress, flag it. */
  onComplete: () => void;
}) {
  const claudeReady = health ? health.found && !health.broken && health.signed_in !== false : true;

  return (
    <div className="flex flex-col gap-8">
      <StepHeader
        eyebrow={eyebrow}
        title="Build your first app"
        lead="Describe what you want. Claude Code names it, scaffolds a folder under ~/Fused/local and starts building — you land on the app with the session streaming beside it."
      />

      {!claudeReady && (
        <div className="flex items-start gap-2 rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-sm">
          <AlertTriangle className="mt-0.5 size-4 shrink-0 text-amber-600 dark:text-amber-400" />
          <span>
            Claude Code isn't connected yet. The app folder will be created, but the build can't
            start until it is — go back a step, or finish setup later from the Home page.
          </span>
        </div>
      )}

      <div className="onboarding-composer">
        <HeroComposer onCreated={onComplete} autoFocus />
      </div>
    </div>
  );
}
