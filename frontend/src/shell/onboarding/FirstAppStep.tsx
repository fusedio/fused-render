// Step 4 — the first app. The Home composer as-is (it names, scaffolds, starts
// the Claude run and navigates), or one of the showcase's local-AI apps.
// Either path is the wizard's "complete": this is the only step whose action
// writes anything, so an abandoned wizard leaves no junk.
//
// `onComplete` writes the flag ONLY — it does not close the overlay. The
// wizard completes and closes on the route change an action causes, so a
// composer `task_error` (folder made, Claude didn't start) stays readable in
// place, and a click on a card's export icon (no navigation) completes nothing.
//
// The showcase clone is fire-and-forget at startup and may not have landed
// yet — the same catalog→refresh dance Apps.tsx does forces it, with a
// skeleton row meanwhile.
import { useEffect, useState } from "react";
import { AlertTriangle } from "lucide-react";

import { HeroComposer } from "@apps/builder/HomeHero";
import { getApps, type AppInfo, type ClaudeHealth } from "@platform/lib/api";
import { runCommunity } from "@platform/lib/community";
import { Skeleton } from "@platform/shadcn/ui/skeleton";
import { AppPreviewCard } from "@platform/ui/AppPreviewCard";

import { StepHeader } from "./StepHeader";

type ShowcaseCatalog = { status?: string };

function isLocalAi(app: AppInfo): boolean {
  return app.tag === "showcase" && app.name.startsWith("local-");
}

function useLocalAiShowcase(): AppInfo[] | null {
  const [apps, setApps] = useState<AppInfo[] | null>(null);
  useEffect(() => {
    let alive = true;
    const fetchGrid = () =>
      getApps().then(
        ({ apps }) => {
          if (alive) setApps(apps.filter(isLocalAi));
        },
        () => {
          if (alive) setApps([]);
        },
      );
    fetchGrid();
    (async () => {
      const local = await runCommunity<ShowcaseCatalog>({ action: "catalog" });
      if (!alive || local.status !== "no-cache") return;
      await runCommunity<ShowcaseCatalog>({ action: "refresh" });
      if (alive) fetchGrid();
    })().catch(() => undefined);
    return () => {
      alive = false;
    };
  }, []);
  return apps;
}

export function FirstAppStep({
  health,
  eyebrow,
  onComplete,
}: {
  health: ClaudeHealth | null;
  eyebrow: string;
  /** The composer created a folder — real progress, flag it. Showcase cards
      need no hook: the navigation they perform is what the wizard reads. */
  onComplete: () => void;
}) {
  const showcase = useLocalAiShowcase();
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
        <HeroComposer onCreated={onComplete} />
      </div>

      <section className="flex flex-col gap-3">
        <div className="flex items-baseline justify-between">
          <h3 className="text-sm font-medium">Or start from a showcase app</h3>
          <span className="text-xs text-muted-foreground">
            Runs AI on this machine — the first open downloads the model.
          </span>
        </div>
        {showcase === null ? (
          <div className="grid gap-4 sm:grid-cols-2">
            {[0, 1, 2, 3].map((i) => (
              <Skeleton key={i} className="aspect-[16/10] w-full rounded-xl" />
            ))}
          </div>
        ) : showcase.length === 0 ? (
          <p className="rounded-xl border border-dashed border-border p-6 text-center text-sm text-muted-foreground">
            The showcase is still downloading — it appears under Apps once it lands.
          </p>
        ) : (
          <div className="grid gap-4 sm:grid-cols-2">
            {showcase.slice(0, 4).map((app) => (
              <AppPreviewCard key={app.path} app={app} />
            ))}
          </div>
        )}
      </section>
    </div>
  );
}
