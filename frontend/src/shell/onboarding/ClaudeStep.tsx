import { useEffect, type ReactNode } from "react";
// Step 2 — Claude Code. A CHECKLIST, not the strip: the strip renders only
// what is wrong and nothing when all is well, which is right for a page
// header and wrong for a setup step, where "installed ✓, signed in ✓" is the
// reassurance the step exists to give. So the four facts are rows, each done
// (struck through, green check) or open (the strip's own IssueRow attached —
// same buttons, same endpoints, same polls, via lib/claude-setup).
//
// Never blocks Next. An unknown (the probe could not tell) is a muted row,
// not a gate — flow's wizard blocked Next on an unknown and had a state with
// no way through.
import { Check, Circle, Minus, RefreshCw } from "lucide-react";

import type { ClaudeHealth } from "@platform/lib/api";
import { claudeIssues, type ClaudeIssue } from "@platform/lib/claude-health";
import type { ClaudeSetup } from "@platform/lib/claude-setup";
import { Button } from "@platform/shadcn/ui/button";
import { Skeleton } from "@platform/shadcn/ui/skeleton";
import { IssueRow } from "@platform/ui/ClaudeHealthStrip";

import { reportStage, type StageStatus } from "./progress";
import { StepHeader } from "./StepHeader";

type RowState = "done" | "open" | "unknown";

interface Row {
  id: "installed" | "version" | "signed-in" | "path";
  label: string;
  hint: string;
  optional?: boolean;
  state: RowState;
  /** Which strip issues belong to this row — the first present one renders. */
  issueIds: ClaudeIssue["id"][];
}

// WHO is signed in, when the CLI said. "as <email> · <org> (<plan>)" for a
// claude.ai login, "Using an API key" for a key, the generic line otherwise —
// so the green check names the account it is vouching for.
function signedInHint(h: ClaudeHealth): string {
  const a = h.account;
  if (h.signed_in !== true || !a) return "Uses your existing Claude subscription — no key to paste.";
  if (a.method === "apiKey") return "Using an API key from the environment (ANTHROPIC_API_KEY).";
  if (a.email) {
    const where = [a.org, a.plan].filter(Boolean).join(" · ");
    return `Signed in as ${a.email}${where ? ` (${where})` : ""}.`;
  }
  if (a.method === "oauthToken") return "Using an OAuth token from the environment.";
  return a.method ? `Signed in via ${a.method}.` : "Signed in.";
}

function rowsFor(h: ClaudeHealth): Row[] {
  const runnable = h.found && !h.broken;
  return [
    {
      id: "installed",
      label: "Claude Code installed",
      hint: "The `claude` command-line tool, from Anthropic.",
      state: runnable ? "done" : "open",
      issueIds: ["missing", "unusable-override", "broken"],
    },
    {
      id: "version",
      label: `Version ${h.min_version} or newer`,
      hint: h.version ? `Found ${h.version}.` : "Could not read the version.",
      state: !runnable ? "open" : h.version == null ? "unknown" : h.outdated ? "open" : "done",
      issueIds: ["outdated"],
    },
    {
      id: "signed-in",
      label: "Signed in",
      hint: signedInHint(h),
      state: !runnable ? "open" : h.signed_in === true ? "done" : h.signed_in === false ? "open" : "unknown",
      issueIds: ["signed-out"],
    },
    {
      id: "path",
      label: "Available in your terminal",
      hint: "Optional. The app works either way; this is for typing `claude` yourself.",
      optional: true,
      // `null` is "could not tell" (Windows, an override) — never a green check.
      state: !runnable ? "open" : h.on_shell_path === false ? "open" : h.on_shell_path === true ? "done" : "unknown",
      issueIds: ["not-on-path"],
    },
  ];
}

function StateIcon({ state, optional }: { state: RowState; optional?: boolean }) {
  if (state === "done")
    return (
      <span className="grid size-6 shrink-0 place-items-center rounded-full bg-emerald-500/15 text-emerald-600 dark:text-emerald-400">
        <Check className="size-3.5" strokeWidth={3} />
      </span>
    );
  if (state === "unknown" || optional)
    return (
      <span className="grid size-6 shrink-0 place-items-center rounded-full border border-border text-muted-foreground">
        <Minus className="size-3" />
      </span>
    );
  return (
    <span className="grid size-6 shrink-0 place-items-center rounded-full border border-amber-500/50 text-amber-600 dark:text-amber-400">
      <Circle className="size-2.5 fill-current" />
    </span>
  );
}

// `setup` is the wizard's single machine (OnboardingWizard owns it, so what
// gets fixed here is what step 4 reads).
export function ClaudeStep({
  setup,
  eyebrow,
  onWork,
}: {
  setup: ClaudeSetup;
  eyebrow: ReactNode;
  onWork: (busy: boolean) => void;
}) {
  const { health, loaded, busy, load } = setup;
  const issues = claudeIssues(health);
  const rows = health ? rowsFor(health) : null;
  // OPTIONAL ROWS ARE NOT WORK. PATH is a convenience — the app is finished
  // with Claude Code without it — so "done" here means every REQUIRED row is
  // done, whatever the optional one says. Counting it made the step look
  // unfinished forever on the many machines whose shell rc we never edit.
  const required = rows?.filter((r) => !r.optional);
  const allDone = required?.every((r) => r.state === "done");
  const optionalOpen = rows?.some((r) => r.optional && r.state === "open");
  // Tell the wizard whether this step still has a button of its own to press:
  // while it does (the strip's yellow actions), Next is not the yellow one.
  // Only a REQUIRED row can claim the accent — the optional row's button is
  // quiet (lib/claude-health `optional`), so Next stays the one yellow button
  // once the three that matter are green.
  const anyActionable = required?.some(
    (r) => r.state === "open" && issues.some((i) => r.issueIds.includes(i.id)),
  );
  useEffect(() => onWork(anyActionable === true), [onWork, anyActionable]);
  // THE STAGE (progress.ts): complete when every required row is done;
  // partial when the CLI runs but a required row is open or unknown (an
  // unknown is never a green check — same rule the PATH row states);
  // pending when it does not run at all. Nothing until health has answered.
  const runnable = health ? health.found && !health.broken : false;
  const stage: StageStatus | null = !health ? null : allDone ? "complete" : runnable ? "partial" : "pending";
  useEffect(() => {
    if (!health || !stage) return;
    reportStage("claude", stage, {
      version: health.version,
      outdated: health.outdated,
      signed_in: health.signed_in,
      account: health.account?.email ?? health.account?.method ?? null,
      on_shell_path: health.on_shell_path,
    });
  }, [stage, health]);

  return (
    <div className="flex flex-col gap-6">
      <StepHeader
        eyebrow={eyebrow}
        title="Connect Claude Code"
        lead={
          <>
            FusedRender builds apps by handing your brief to Claude Code running on
            this machine. It is what the composer, the Tasks page and every{" "}
            <code className="rounded bg-muted px-1 py-0.5 text-[0.85em]">fused.ai</code>{" "}
            call go through. The file explorer, previews and local models work without
            it — you can finish this later.
          </>
        }
      />

      <div className="flex items-center justify-between">
        <p className="text-sm text-muted-foreground" role="status">
          {!loaded
            ? "Checking this machine…"
            : anyActionable
              ? "A few things still need doing — the open rows have buttons."
              : allDone
                ? optionalOpen
                  ? "Claude Code is ready. The last row is optional."
                  : "Everything is in place."
                : "Nothing here will block you — carry on."}
        </p>
        <Button variant="outline" size="sm" onClick={() => load(true)} disabled={busy}>
          <RefreshCw data-icon="inline-start" className={busy ? "animate-spin" : undefined} />
          {busy ? "Checking…" : "Check again"}
        </Button>
      </div>

      <ol className="m-0 flex list-none flex-col divide-y divide-border rounded-xl border border-border bg-card p-0">
        {!rows &&
          [0, 1, 2, 3].map((i) => (
            <li key={i} className="flex items-center gap-3 px-4 py-3">
              <Skeleton className="size-6 rounded-full" />
              <Skeleton className="h-4 w-48" />
            </li>
          ))}
        {rows?.map((row) => {
          const issue = row.state === "open" ? issues.find((i) => row.issueIds.includes(i.id)) : undefined;
          return (
            <li key={row.id} className="flex flex-col gap-2 px-4 py-3">
              <div className="flex items-start gap-3">
                <StateIcon state={row.state} optional={row.optional && row.state !== "done"} />
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2 text-sm font-medium">
                    <span className={row.state === "done" ? "text-muted-foreground line-through" : undefined}>
                      {row.label}
                    </span>
                    {row.optional && (
                      <span className="rounded-full border border-border px-1.5 py-px text-[11px] font-normal text-muted-foreground">
                        optional
                      </span>
                    )}
                  </div>
                  <div className="text-xs text-muted-foreground">
                    {row.state === "unknown" ? "Couldn't check — carry on, it will not block you." : row.hint}
                  </div>
                </div>
              </div>
              {issue && (
                <ul className="claude-health-issues onboarding-issue ml-9">
                  <IssueRow
                    issue={issue}
                    install={setup.install}
                    login={setup.login}
                    doctor={issue.id === "broken" ? setup.doctor : null}
                    onAct={setup.act}
                    onCancelLogin={setup.cancelLogin}
                    busy={setup.acting}
                    actionError={issue.action ? setup.actionError : null}
                    doneNote={issue.id === "not-on-path" ? setup.linkedNote : null}
                  />
                </ul>
              )}
            </li>
          );
        })}
      </ol>
    </div>
  );
}
