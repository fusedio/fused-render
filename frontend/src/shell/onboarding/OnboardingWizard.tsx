// The first-run wizard: a full-screen takeover, four steps, every one of them
// skippable. Mounted at the app root (App.tsx) above every route, because a
// pre-app surface cannot live inside one.
//
//   1 About        — what FusedRender is (download-page copy + video)
//   2 Claude Code  — installed / new enough / signed in / on PATH, with buttons
//   3 Disk Access  — macOS Full Disk Access, why, and the one button there is
//   4 First app    — the Home composer, or a showcase local-AI app
//
// Steps 1–3 write nothing. Step 4's create (or a showcase open) is the only
// durable action and doubles as "complete". "Skip for now" / ✕ / Escape record
// a DISMISS — a different flag, so a later build can tell the two apart. Both
// stop the auto-show; neither is undone by reopening from Help › Setup.
//
// While open, App.tsx holds the auto-tours (shell/onboarding/state); they fire
// on the route the user lands on once this closes.
import { useCallback, useEffect, useState } from "react";
import { ArrowLeft, ArrowRight, Rocket, ShieldCheck, Sparkles, TerminalSquare, X } from "lucide-react";

import { completeOnboarding, dismissOnboarding, type Config } from "@platform/lib/api";
import { useClaudeSetup } from "@platform/lib/claude-setup";
import { Button } from "@platform/shadcn/ui/button";
import { cn } from "@platform/lib/utils";

import { AboutStep } from "./AboutStep";
import { ClaudeStep } from "./ClaudeStep";
import { FdaStep } from "./FdaStep";
import { FirstAppStep } from "./FirstAppStep";
import { closeOnboarding, openOnboarding, shouldAutoShow, useOnboardingOpen } from "./state";

type StepId = "about" | "claude" | "fda" | "app";

const STEPS: { id: StepId; label: string; icon: React.ReactNode }[] = [
  { id: "about", label: "About", icon: <Sparkles className="size-3.5" /> },
  { id: "claude", label: "Claude Code", icon: <TerminalSquare className="size-3.5" /> },
  { id: "fda", label: "Disk Access", icon: <ShieldCheck className="size-3.5" /> },
  { id: "app", label: "First app", icon: <Rocket className="size-3.5" /> },
];

// Full Disk Access is a macOS concept. The server says which platform it is
// on (claude health carries `platform`); until that lands, the browser's own
// hint decides so the strip does not jump when the probe answers.
function isMac(platform: string | null | undefined): boolean {
  if (platform) return platform === "darwin";
  return /Mac/i.test(navigator.platform || navigator.userAgent);
}

export function OnboardingWizard({ config }: { config: Config }) {
  const open = useOnboardingOpen();

  // Auto-show once, from the flag the server handed over at boot.
  useEffect(() => {
    if (shouldAutoShow(config)) openOnboarding();
    // eslint-disable-next-line react-hooks/exhaustive-deps -- boot-time decision
  }, []);

  if (!open) return null;
  return <Wizard config={config} />;
}

function Wizard({ config }: { config: Config }) {
  const { health } = useClaudeSetup(false);
  const steps = STEPS.filter((s) => s.id !== "fda" || isMac(health?.platform));
  const [index, setIndex] = useState(0);
  const step = steps[Math.min(index, steps.length - 1)];
  const last = index >= steps.length - 1;

  const finish = useCallback((how: "complete" | "dismiss") => {
    // Fire-and-forget: the flag is a courtesy to the NEXT launch, and a failed
    // write must not hold the overlay over the app the user is trying to reach.
    (how === "complete" ? completeOnboarding() : dismissOnboarding()).catch(() => undefined);
    closeOnboarding();
  }, []);

  const next = useCallback(() => {
    if (last) finish("complete");
    else setIndex((i) => i + 1);
  }, [last, finish]);
  const back = () => setIndex((i) => Math.max(0, i - 1));

  // Escape dismisses; ⌘/Ctrl+Enter advances — except on the last step, where
  // the composer owns Enter and "complete" is the create itself.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        finish("dismiss");
      } else if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && !last) {
        e.preventDefault();
        next();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [finish, next, last]);

  // Body scroll stays put underneath.
  useEffect(() => {
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = prev;
    };
  }, []);

  return (
    <div
      className="onboarding fixed inset-0 z-[60] flex flex-col bg-background text-foreground"
      role="dialog"
      aria-modal="true"
      aria-label="Set up FusedRender"
    >
      {/* Top bar: brand · progress · skip */}
      <div className="flex items-center gap-4 border-b border-border px-5 py-3">
        <div className="flex min-w-0 items-center gap-2 text-sm font-medium">
          <span className="grid size-6 place-items-center rounded-md bg-primary text-[11px] font-bold text-primary-foreground">
            F
          </span>
          <span className="truncate">Set up FusedRender</span>
        </div>

        <ol className="mx-auto hidden items-center gap-1 sm:flex" aria-label="Setup steps">
          {steps.map((s, i) => {
            const done = i < index;
            const current = i === index;
            return (
              <li key={s.id} className="flex items-center">
                <button
                  type="button"
                  onClick={() => i <= index && setIndex(i)}
                  disabled={i > index}
                  aria-current={current ? "step" : undefined}
                  className={cn(
                    "flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs transition-colors",
                    current && "bg-foreground text-background",
                    done && "text-foreground hover:bg-muted",
                    !current && !done && "text-muted-foreground/60 cursor-default",
                  )}
                >
                  {s.icon}
                  {s.label}
                </button>
                {i < steps.length - 1 && <span className="mx-1 h-px w-4 bg-border" aria-hidden />}
              </li>
            );
          })}
        </ol>

        <div className="ml-auto flex items-center gap-1 sm:ml-0">
          <Button variant="ghost" size="sm" onClick={() => finish("dismiss")}>
            Skip for now
          </Button>
          <Button variant="ghost" size="icon-sm" onClick={() => finish("dismiss")} aria-label="Close setup">
            <X />
          </Button>
        </div>
      </div>

      {/* Body */}
      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className={cn("mx-auto w-full px-6 py-10", step.id === "app" ? "max-w-4xl" : "max-w-2xl")}>
          {step.id === "about" && <AboutStep />}
          {step.id === "claude" && <ClaudeStep />}
          {step.id === "fda" && <FdaStep config={config} />}
          {step.id === "app" && <FirstAppStep health={health} onComplete={() => finish("complete")} />}
        </div>
      </div>

      {/* Footer */}
      <div className="flex items-center justify-between border-t border-border px-5 py-3">
        <Button variant="ghost" size="sm" onClick={back} disabled={index === 0}>
          <ArrowLeft data-icon="inline-start" />
          Back
        </Button>
        <div className="text-xs text-muted-foreground sm:hidden">
          {index + 1} / {steps.length}
        </div>
        {last ? (
          <Button variant="outline" size="sm" onClick={() => finish("complete")}>
            I'll explore on my own
          </Button>
        ) : (
          <Button size="sm" onClick={next} title="⌘/Ctrl + Enter">
            Next
            <ArrowRight data-icon="inline-end" />
          </Button>
        )}
      </div>
    </div>
  );
}

export default OnboardingWizard;
