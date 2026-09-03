// The first-run wizard: the `/onboarding` page, four steps, every one of them
// skippable. App.tsx renders it ALONE on that route — no sidebar, no status
// bar — and redirects a fresh install there at boot (shell/onboarding/state
// has the rule). Leaving is a navigation like any other.
//
//   1 About        — what FusedRender is (download-page copy + video)
//   2 Claude Code  — installed / new enough / signed in / on PATH, with buttons
//   3 Disk Access  — macOS Full Disk Access, why, and the one button there is
//   4 First app    — the Home composer, or a showcase local-AI app
//
// Steps 1–3 write nothing. Step 4's create (or a showcase open) is the only
// durable action and doubles as "complete". ✕ / Escape record a DISMISS — a
// different flag, so a later build can tell the two apart. Both stop the
// auto-show; neither is undone by reopening from Help › Setup wizard.
import { useCallback, useEffect, useRef, useState } from "react";
import { ArrowLeft, ArrowRight, Check, X } from "lucide-react";

import { completeOnboarding, dismissOnboarding, type Config } from "@platform/lib/api";
import { useClaudeSetup } from "@platform/lib/claude-setup";
import { navigateUrl, replaceSearch } from "@platform/lib/router";
import { Button } from "@platform/shadcn/ui/button";
import { cn } from "@platform/lib/utils";
import { FusedMark } from "@platform/ui/FusedMark";

import { AboutStep } from "./AboutStep";
import { ClaudeStep } from "./ClaudeStep";
import { FdaStep } from "./FdaStep";
import { FirstAppStep } from "./FirstAppStep";

type StepId = "about" | "claude" | "fda" | "app";

const STEPS: { id: StepId; label: string }[] = [
  { id: "about", label: "About" },
  { id: "claude", label: "Claude Code" },
  { id: "fda", label: "Disk Access" },
  { id: "app", label: "First app" },
];

// The step id rides in the query so a refresh and a link both land on it.
const STEP_PARAM = "step";
function stepFromUrl(): StepId | null {
  const v = new URLSearchParams(location.search).get(STEP_PARAM);
  return STEPS.some((s) => s.id === v) ? (v as StepId) : null;
}

// Where the wizard lets go: the front door.
const EXIT_PATH = "/home";

// Full Disk Access is a macOS concept. The server says which platform it is
// on (claude health carries `platform`); until that lands, the browser's own
// hint decides so the strip does not jump when the probe answers.
function isMac(platform: string | null | undefined): boolean {
  if (platform) return platform === "darwin";
  return /Mac/i.test(navigator.platform || navigator.userAgent);
}

export function OnboardingWizard({ config }: { config: Config }) {
  // The step is named in the URL (`/onboarding?step=claude`): a refresh stays
  // put, a link can point at one step, and the id — not a position — is what
  // is held, so the FDA step appearing once health answers "darwin" does not
  // shift the page under the user. Mirrored with replaceState: steps are not
  // history entries, Back leaves the wizard.
  const [stepId, setStepId] = useState<StepId>(() => stepFromUrl() ?? "about");
  useEffect(() => {
    const url = new URL(location.href);
    if (url.searchParams.get(STEP_PARAM) === stepId) return;
    url.searchParams.set(STEP_PARAM, stepId);
    replaceSearch(url.pathname + url.search);
  }, [stepId]);
  // ONE setup machine for the whole wizard, not one per step: a sign-in done
  // on step 2 must be what step 4 reads, and two hook instances would each
  // hold their own snapshot. Focus re-checks only while the Claude step is up.
  const setup = useClaudeSetup(stepId === "claude");
  const { health } = setup;
  const steps = STEPS.filter((s) => s.id !== "fda" || isMac(health?.platform));
  // A URL naming a step this machine does not have (`fda` off macOS) lands on
  // the first one rather than nowhere.
  const found = steps.findIndex((s) => s.id === stepId);
  const index = found < 0 ? 0 : found;
  const step = steps[index];
  const last = index >= steps.length - 1;
  const setIndex = (i: number) => setStepId(steps[Math.max(0, Math.min(i, steps.length - 1))].id);
  // Counted over the steps this machine actually has (no FDA off macOS).
  const eyebrow = `Step ${index + 1} of ${steps.length}`;

  // Fire-and-forget, and at most one flag per visit: the flag is a courtesy
  // to the NEXT launch, and a failed write must not hold the page over the
  // app the user is trying to reach.
  const settled = useRef(false);
  const markComplete = useCallback(() => {
    if (settled.current) return;
    settled.current = true;
    completeOnboarding().catch(() => undefined);
  }, []);
  const finish = useCallback(
    (how: "complete" | "dismiss") => {
      if (how === "complete") markComplete();
      else if (!settled.current) {
        settled.current = true;
        dismissOnboarding().catch(() => undefined);
      }
      navigateUrl(EXIT_PATH);
    },
    [markComplete],
  );

  // Step 4's actions (the composer's create, a showcase card) NAVIGATE — App
  // swaps this page out on the route change, not when the action starts, so
  // the composer's own `task_error` ("folder created, Claude didn't start")
  // lands in a composer that is still on screen where the user can read it.
  //
  // A navigation off the last step IS completion (a showcase card opened, or
  // the composer landed on the new app) — so cards need no click hook, and a
  // click that does not navigate (a card's export icon) completes nothing.
  // The unmount is that navigation.
  const lastRef = useRef(last);
  lastRef.current = last;
  useEffect(
    () => () => {
      if (lastRef.current) markComplete();
    },
    [markComplete],
  );

  const next = () => {
    if (last) finish("complete");
    else setIndex(index + 1);
  };
  const back = () => setIndex(index - 1);

  // Escape dismisses; ⌘/Ctrl+Enter advances. Neither on the last step: the
  // composer owns both there (Escape cancels its name prompt, Enter sends).
  useEffect(() => {
    if (last) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        finish("dismiss");
      } else if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        next();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- `next` is a per-render closure over index
  }, [finish, last, index, steps.length]);

  return (
    <div
      className="onboarding flex min-h-0 flex-1 flex-col bg-background text-foreground"
      aria-label="Set up FusedRender"
    >
      {/* Top bar: brand · steps · close */}
      <div className="flex items-center gap-4 border-b border-border px-5 py-3">
        <div className="flex min-w-0 items-center gap-2 text-[13.5px] font-semibold tracking-[0.01em]">
          <span className="flex shrink-0 items-center text-[var(--accent)]">
            <FusedMark size={20} />
          </span>
          <span className="truncate">Fused Render</span>
        </div>

        {/* Every step is a link, in both directions: nothing before step 4
            gates anything after it, so a user who knows what they want can go
            straight there. */}
        <ol
          className="mx-auto my-0 hidden list-none items-center gap-0.5 rounded-lg bg-muted/60 p-0.5 sm:flex"
          aria-label="Setup steps"
        >
          {steps.map((s, i) => {
            const done = i < index;
            const current = i === index;
            return (
              <li key={s.id} className="flex items-center">
                <button
                  type="button"
                  onClick={() => setStepId(s.id)}
                  aria-current={current ? "step" : undefined}
                  className={cn(
                    "flex cursor-pointer appearance-none items-center gap-1.5 rounded-md border-0 bg-transparent px-3 py-1.5 text-xs leading-none [font-family:inherit] transition-colors outline-none focus-visible:ring-2 focus-visible:ring-ring",
                    current
                      ? "bg-background font-medium text-foreground shadow-sm"
                      : "text-muted-foreground hover:text-foreground",
                  )}
                >
                  <span
                    className={cn(
                      "grid size-4 place-items-center rounded-full text-[10px] font-semibold tabular-nums",
                      current && "bg-foreground text-background",
                      done && "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400",
                      !current && !done && "bg-muted-foreground/15 text-muted-foreground",
                    )}
                    aria-hidden
                  >
                    {done ? <Check className="size-2.5" strokeWidth={3} /> : i + 1}
                  </span>
                  {s.label}
                </button>
              </li>
            );
          })}
        </ol>

        <button
          type="button"
          onClick={() => finish("dismiss")}
          aria-label="Close setup"
          title="Skip for now"
          className="ml-auto grid size-8 cursor-pointer appearance-none place-items-center rounded-md border-0 bg-transparent text-muted-foreground transition-colors outline-none hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring sm:ml-0"
        >
          <X className="size-4" />
        </button>
      </div>

      {/* Body */}
      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto w-full max-w-4xl px-6 py-10">
          {step.id === "about" && <AboutStep eyebrow={eyebrow} />}
          {step.id === "claude" && <ClaudeStep setup={setup} eyebrow={eyebrow} />}
          {step.id === "fda" && <FdaStep config={config} eyebrow={eyebrow} />}
          {step.id === "app" && <FirstAppStep health={health} eyebrow={eyebrow} onComplete={markComplete} />}
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
          <Button key="explore" variant="outline" size="sm" onClick={() => finish("complete")}>
            I'll explore on my own
          </Button>
        ) : (
          <Button key="next" size="sm" onClick={next} title="⌘/Ctrl + Enter">
            Next
            <ArrowRight data-icon="inline-end" />
          </Button>
        )}
      </div>
    </div>
  );
}

export default OnboardingWizard;
